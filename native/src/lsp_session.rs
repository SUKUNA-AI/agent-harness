//! Linux-only, per-Session Tokio byte transport. Python owns LSP semantics.
//! The helper is an already registered, unreaped SID/PGID leader. The language
//! server inherits that group. On parent EOF/failure we terminate the entire
//! owned group, including this helper; runtime Drop cannot hang on stdio workers.
use serde_json::{json,Value};

pub const MAX_FRAME_BYTES: usize = 16*1024*1024;
pub const MAX_WIRE_BYTES: usize = 2*MAX_FRAME_BYTES+4096;
pub const MAX_CONTROL_BYTES: usize = 1024*1024;
pub const MAX_STREAM_BYTES: usize = 64*1024*1024;
pub const MAX_WIRE_STREAM: usize = 256*1024*1024;
pub const MAX_PACKETS: usize = 40_000;
pub const MAX_RPC_NODES: usize = 1_000_000;
const MAX_HEADERS: usize = 8192;

pub fn capability() -> Value {
    if cfg!(target_os="linux") {json!({"mode":"lsp-session","protocol_version":1,"platform":"linux"})}
    else {Value::Null}
}
pub fn limits() -> Value {
    json!({"max_lsp_frame_bytes":MAX_FRAME_BYTES,"max_header_bytes":MAX_HEADERS,
        "max_lsp_stream_bytes":MAX_STREAM_BYTES,"max_lsp_frames":20000,
        "max_wire_frame_bytes":MAX_WIRE_BYTES,"max_control_bytes":MAX_CONTROL_BYTES,
        "max_wire_stream_bytes":MAX_WIRE_STREAM,"max_wire_packets":MAX_PACKETS,
        "max_rpc_nodes":MAX_RPC_NODES,"queue_slots":4,"queue_decoded_bytes":MAX_FRAME_BYTES,
        "input_metadata_reserve_bytes":4096,
        "stderr_chunk_bytes":4096,"stderr_tail_bytes":65536})
}
fn closed(value:&Value,fields:&[&str]) -> Result<(),&'static str> {
    let map=value.as_object().ok_or("invalid_input")?;
    if map.len()!=fields.len() || !fields.iter().all(|field|map.contains_key(*field)) {return Err("invalid_input");}
    Ok(())
}
fn valid_session(session:&str) -> bool {
    !session.is_empty() && session.len()<=128 && session.bytes().all(|v|v.is_ascii_alphanumeric() || b"-_.:".contains(&v))
}
fn rpc(raw:&[u8]) -> Result<(),&'static str> {
    let value=crate::protocol::parse_bounded(raw,MAX_FRAME_BYTES,128,MAX_RPC_NODES).map_err(|_|"invalid_jsonrpc")?;
    let map=value.as_object().ok_or("invalid_jsonrpc")?;
    if map.get("jsonrpc").and_then(Value::as_str)!=Some("2.0") {return Err("invalid_jsonrpc");}
    if let Some(id)=map.get("id") {
        let valid=id.is_i64() || id.is_u64() || id.as_str().is_some_and(|v|v.len()<=1024)
            || (id.is_null() && !map.contains_key("method") && map.contains_key("error"));
        if !valid {return Err("invalid_jsonrpc_id");}
    }
    if let Some(method)=map.get("method") {
        if !method.as_str().is_some_and(|v|!v.is_empty() && v.len()<=1024)
            || map.contains_key("result") || map.contains_key("error") {return Err("invalid_jsonrpc");}
    } else {
        if !map.contains_key("id") || map.contains_key("result")==map.contains_key("error") {return Err("invalid_jsonrpc");}
        if let Some(error)=map.get("error") {
            if !error.is_object() || error.get("code").and_then(Value::as_i64).is_none()
                || error.get("message").and_then(Value::as_str).is_none() {return Err("invalid_jsonrpc");}
        }
    }
    Ok(())
}

#[cfg(not(target_os="linux"))]
pub fn run_cli(_session:&str) -> ! {
    eprintln!("LSP native transport is not qualified on this platform");std::process::exit(2)
}

#[cfg(target_os="linux")]
pub fn run_cli(session:&str) -> ! { linux::run(session) }

#[cfg(target_os="linux")]
mod linux {
    use super::*;
    use std::process::Stdio;
    use std::sync::Arc;
    use std::time::Duration;
    use tokio::io::{AsyncBufRead,AsyncBufReadExt,AsyncRead,AsyncReadExt,AsyncWrite,AsyncWriteExt,BufReader};
    use tokio::sync::{mpsc,oneshot,OwnedSemaphorePermit,Semaphore};
    use tokio::time::{Instant,timeout};

    fn owned_group() -> Result<i32,&'static str> {
        // SAFETY: these are scalar, side-effect-free identity queries.
        let pid=unsafe {libc::getpid()};
        if pid<=1 || unsafe {libc::getpgrp()}!=pid || unsafe {libc::getsid(0)}!=pid {return Err("unowned_process_group");}
        Ok(pid)
    }
    fn terminate_group(pgid:i32) -> ! {
        if owned_group()==Ok(pgid) {
            // SAFETY: this helper is the live group leader. Its PID cannot be
            // reused while executing, and this signal only targets its group.
            unsafe {libc::kill(-pgid,libc::SIGKILL);}
        }
        // Never unwind/drop a runtime containing blocking stdin/stdout workers.
        std::process::exit(74)
    }
    fn parent_closed() -> bool {
        let mut descriptor=libc::pollfd {fd:0,events:0,revents:0};
        // POLLHUP is visible even when unread control data remains in the pipe.
        // This check stays independent of all data queues/backpressure.
        (unsafe {libc::poll(&mut descriptor,1,0)})<0 || descriptor.revents&(libc::POLLHUP|libc::POLLERR|libc::POLLNVAL)!=0
    }
    fn birth() -> Result<String,&'static str> {
        use std::io::Read;
        let mut bytes=String::new();std::fs::File::open("/proc/self/stat").map_err(|_|"helper_identity_unavailable")?
            .take(8193).read_to_string(&mut bytes).map_err(|_|"helper_identity_unavailable")?;
        if bytes.len()>8192 {return Err("helper_identity_unavailable");}
        bytes.rsplit_once(')').and_then(|(_,tail)|tail.split_whitespace().nth(19)).map(str::to_owned).ok_or("helper_identity_unavailable")
    }
    #[derive(Default)]
    struct Counter {bytes:usize,packets:usize}
    impl Counter {
        fn add(&mut self,size:usize)->Result<(),&'static str> {
            self.bytes=self.bytes.checked_add(size+4).ok_or("wire_capacity_exceeded")?;
            self.packets+=1;
            if self.bytes>MAX_WIRE_STREAM || self.packets>MAX_PACKETS {return Err("wire_capacity_exceeded");}
            Ok(())
        }
    }
    async fn read_wire<R:AsyncRead+Unpin>(reader:&mut R,cap:usize,counter:&mut Counter)->Result<Vec<u8>,&'static str> {
        let mut prefix=[0u8;4];reader.read_exact(&mut prefix).await.map_err(|_|"parent_eof")?;
        let length=u32::from_be_bytes(prefix) as usize;
        if length==0 || length>cap {return Err("wire_frame_too_large");}
        counter.add(length)?;
        let mut bytes=vec![0u8;length];reader.read_exact(&mut bytes).await.map_err(|_|"truncated_wire_frame")?;
        Ok(bytes)
    }
    async fn write_wire<W:AsyncWrite+Unpin>(writer:&mut W,value:&Value,counter:&mut Counter)->Result<(),&'static str> {
        let raw=serde_json::to_vec(value).map_err(|_|"wire_encode_failed")?;
        if raw.len()>MAX_WIRE_BYTES {return Err("wire_frame_too_large");}
        counter.add(raw.len())?;
        writer.write_all(&(raw.len() as u32).to_be_bytes()).await.map_err(|_|"parent_output_closed")?;
        writer.write_all(&raw).await.map_err(|_|"parent_output_closed")?;
        writer.flush().await.map_err(|_|"parent_output_closed")
    }
    fn common(value:&Value,session:&str,sequence:u64)->Result<(),&'static str> {
        if value.get("protocol_version").and_then(Value::as_u64)!=Some(1)
            || value.get("session_id").and_then(Value::as_str)!=Some(session)
            || value.get("sequence").and_then(Value::as_u64)!=Some(sequence) {return Err("wire_binding_conflict");}
        Ok(())
    }
    struct Start {argv:Vec<String>,cwd:String,timeout_ms:u64}
    fn start(value:&Value,session:&str)->Result<Start,&'static str> {
        closed(value,&["protocol_version","session_id","sequence","kind","argv","cwd","timeout_ms"])?;
        common(value,session,1)?;
        if value["kind"]!="start" {return Err("invalid_start");}
        let args=value["argv"].as_array().filter(|v|!v.is_empty() && v.len()<=64).ok_or("invalid_argv")?;
        let mut argv=Vec::new();
        for arg in args {
            let arg=arg.as_str().filter(|v|v.len()<=8192 && !v.contains('\0')).ok_or("invalid_argv")?;
            argv.push(arg.to_owned());
        }
        if argv[0].is_empty() {return Err("invalid_argv");}
        let cwd=value["cwd"].as_str().filter(|v|!v.is_empty() && v.len()<=65_536 && !v.contains('\0')
            && std::path::Path::new(v).is_absolute()).ok_or("invalid_cwd")?.to_owned();
        let timeout_ms=value["timeout_ms"].as_u64().filter(|v|*v>0 && *v<=120_000).ok_or("invalid_timeout")?;
        Ok(Start {argv,cwd,timeout_ms})
    }
    struct Event {value:Value,_permit:Option<OwnedSemaphorePermit>,written:Option<oneshot::Sender<()>>}
    struct ToServer {body:String,_permit:OwnedSemaphorePermit}
    async fn post(sender:&mpsc::Sender<Event>,budget:&Arc<Semaphore>,value:Value,size:usize)->Result<(),&'static str> {
        let permit=budget.clone().acquire_many_owned(size.max(1) as u32).await.map_err(|_|"output_closed")?;
        sender.send(Event {value,_permit:Some(permit),written:None}).await.map_err(|_|"output_closed")
    }
    async fn output(mut events:mpsc::Receiver<Event>,session:String,mut counter:Counter)->Result<(),&'static str> {
        let mut writer=tokio::io::stdout();let mut sequence=1u64;
        while let Some(mut event)=events.recv().await {
            sequence+=1;
            let map=event.value.as_object_mut().ok_or("internal_event_type")?;
            map.insert("protocol_version".into(),json!(1));map.insert("session_id".into(),json!(session));
            map.insert("sequence".into(),json!(sequence));
            write_wire(&mut writer,&event.value,&mut counter).await?;
            if let Some(ack)=event.written.take() {let _=ack.send(());}
        }
        Ok(())
    }
    async fn input<R:AsyncRead+Unpin>(mut reader:R,session:String,mut count:Counter,sender:mpsc::Sender<ToServer>,budget:Arc<Semaphore>)->Result<&'static str,&'static str> {
        let mut sequence=1u64;
        loop {
            let bytes=read_wire(&mut reader,MAX_WIRE_BYTES,&mut count).await?;
            let mut value=crate::protocol::parse_bounded(&bytes,MAX_WIRE_BYTES,128,MAX_RPC_NODES).map_err(|_|"invalid_wire_json")?;
            sequence+=1;common(&value,&session,sequence)?;
            match value.get("kind").and_then(Value::as_str) {
                Some("message")=>{
                    closed(&value,&["protocol_version","session_id","sequence","kind","body"])?;
                    let body=value.as_object_mut().and_then(|map|map.remove("body")).and_then(|v|match v {Value::String(body)=>Some(body),_=>None}).ok_or("invalid_rpc_body")?;
                    rpc(body.as_bytes())?;
                    // Never await a data permit/queue in the control reader:
                    // cancellation and EOF must remain observable under load.
                    // Overload stops the Session explicitly; no message is lost
                    // with a claimed successful continuation.
                    let permit=budget.clone().try_acquire_many_owned(body.len().max(1) as u32).map_err(|_|"input_queue_capacity_exceeded")?;
                    sender.try_send(ToServer {body,_permit:permit}).map_err(|_|"input_queue_capacity_exceeded")?;
                },
                Some("cancel")|Some("close")=>{
                    closed(&value,&["protocol_version","session_id","sequence","kind"])?;
                    return Ok("cancelled");
                },
                _=>return Err("invalid_control_kind"),
            }
        }
    }
    async fn to_server(mut writer:tokio::process::ChildStdin,mut messages:mpsc::Receiver<ToServer>)->Result<(),&'static str> {
        while let Some(message)=messages.recv().await {
            let body=message.body.as_bytes();
            writer.write_all(format!("Content-Length: {}\r\n\r\n",body.len()).as_bytes()).await.map_err(|_|"server_stdin_closed")?;
            writer.write_all(body).await.map_err(|_|"server_stdin_closed")?;
            writer.flush().await.map_err(|_|"server_stdin_closed")?;
        }
        Ok(())
    }
    async fn lsp_frame<R:AsyncBufRead+Unpin>(reader:&mut R)->Result<Option<(String,usize)>,&'static str> {
        let mut used=0usize;let mut length=None;let mut headers=std::collections::BTreeSet::new();
        loop {
            let mut line=Vec::new();
            let mut bounded=(&mut *reader).take((MAX_HEADERS+1-used) as u64);
            let count=bounded.read_until(b'\n',&mut line).await.map_err(|_|"server_stdout_failed")?;
            if count==0 {return if used==0 {Ok(None)} else {Err("truncated_lsp_header")};}
            used+=count;
            if used>MAX_HEADERS || !line.ends_with(b"\r\n") {return Err("invalid_lsp_header");}
            if line==b"\r\n" {break;}
            let line=&line[..line.len()-2];
            let split=line.iter().position(|b|*b==b':').ok_or("invalid_lsp_header")?;
            let name=&line[..split];
            if name.is_empty() || !name.iter().all(|b|b.is_ascii_alphanumeric() || *b==b'-') {return Err("invalid_lsp_header");}
            let name=name.to_ascii_lowercase();if !headers.insert(name.clone()) {return Err("duplicate_lsp_header");}
            let raw=std::str::from_utf8(&line[split+1..]).map_err(|_|"invalid_lsp_header")?.trim_matches([' ','\t']);
            if name==b"content-length" {
                if raw.is_empty() || !raw.bytes().all(|v|v.is_ascii_digit()) {return Err("invalid_content_length");}
                let bytes=raw.parse::<usize>().map_err(|_|"invalid_content_length")?;
                if bytes==0 || bytes>MAX_FRAME_BYTES {return Err("lsp_frame_too_large");}length=Some(bytes);
            }
        }
        let length=length.ok_or("missing_content_length")?;
        let mut body=vec![0u8;length];reader.read_exact(&mut body).await.map_err(|_|"truncated_lsp_body")?;
        rpc(&body)?;
        let text=String::from_utf8(body).map_err(|_|"invalid_utf8")?;
        Ok(Some((text,used+length)))
    }
    async fn from_server(source:tokio::process::ChildStdout,events:mpsc::Sender<Event>,budget:Arc<Semaphore>)->Result<(),&'static str> {
        let mut reader=BufReader::new(source);let mut bytes=0usize;let mut frames=0usize;
        while let Some((body,size))=lsp_frame(&mut reader).await? {
            bytes=bytes.checked_add(size).ok_or("lsp_stream_capacity_exceeded")?;frames+=1;
            if bytes>MAX_STREAM_BYTES || frames>20000 {return Err("lsp_stream_capacity_exceeded");}
            let size=body.len();post(&events,&budget,json!({"kind":"message","body":body}),size).await?;
        }
        Ok(())
    }
    async fn stderr(mut source:tokio::process::ChildStderr,events:mpsc::Sender<Event>,budget:Arc<Semaphore>)->Result<(),&'static str> {
        let mut bytes=[0u8;4096];
        loop {
            let count=source.read(&mut bytes).await.map_err(|_|"server_stderr_failed")?;
            if count==0 {return Ok(());}
            let hex:String=bytes[..count].iter().map(|byte|format!("{byte:02x}")).collect();
            post(&events,&budget,json!({"kind":"stderr","hex":hex}),count).await?;
        }
    }
    async fn session(session:&str,pgid:i32)->Result<(),&'static str> {
        let mut out=tokio::io::stdout();let mut out_count=Counter::default();
        let ready=json!({"protocol_version":1,"session_id":session,"sequence":1,"kind":"ready",
            "helper_pid":pgid,"pgid":pgid,"sid":pgid,"created":birth()?,"version":env!("CARGO_PKG_VERSION"),"platform":"linux",
            "limits":limits()});
        timeout(Duration::from_secs(2),write_wire(&mut out,&ready,&mut out_count)).await.map_err(|_|"ready_timeout")??;
        let mut reader=tokio::io::stdin();let mut in_count=Counter::default();
        let bytes=timeout(Duration::from_secs(2),read_wire(&mut reader,MAX_CONTROL_BYTES,&mut in_count)).await.map_err(|_|"start_timeout")??;
        let value=crate::protocol::parse_bounded(&bytes,MAX_CONTROL_BYTES,32,8192).map_err(|_|"invalid_start_json")?;
        let start=start(&value,session)?;
        let deadline=Instant::now()+Duration::from_millis(start.timeout_ms);
        let mut child=tokio::process::Command::new(&start.argv[0]).args(&start.argv[1..]).current_dir(start.cwd)
            .stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::piped()).kill_on_drop(false)
            .spawn().map_err(|_|"server_spawn_failed")?;
        let child_pid=child.id().ok_or("server_identity_unavailable")?;
        let stdin=child.stdin.take().ok_or("server_stdin_unavailable")?;
        let stdout=child.stdout.take().ok_or("server_stdout_unavailable")?;
        let child_stderr=child.stderr.take().ok_or("server_stderr_unavailable")?;
        let (events_tx,events_rx)=mpsc::channel(4);let output_budget=Arc::new(Semaphore::new(MAX_FRAME_BYTES));
        let mut writer_task=tokio::spawn(output(events_rx,session.to_owned(),out_count));
        post(&events_tx,&output_budget,json!({"kind":"process_started","child_pid":child_pid,"pgid":pgid}),1).await?;
        let (server_tx,server_rx)=mpsc::channel(4);
        let mut input_task=tokio::spawn(input(reader,session.to_owned(),in_count,server_tx,Arc::new(Semaphore::new(MAX_FRAME_BYTES+4096))));
        let mut server_write_task=tokio::spawn(to_server(stdin,server_rx));
        let mut stdout_task=tokio::spawn(from_server(stdout,events_tx.clone(),output_budget.clone()));
        let mut stderr_task=tokio::spawn(stderr(child_stderr,events_tx.clone(),output_budget));
        let mut monitor=tokio::time::interval(Duration::from_millis(50));monitor.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
        let mut stdout_done=false;let mut stderr_done=false;let mut stdout_complete=false;let mut stderr_complete=false;
        let mut child_exit=None;let mut child_reaped=false;
        let reason=loop {
            tokio::select! {
                value=child.wait()=>{child_exit=value.ok().and_then(|v|v.code());child_reaped=true;break "child_exited";},
                result=&mut input_task=>{break match result {Ok(Ok(reason))=>reason,Ok(Err(reason))=>reason,Err(_)=>"control_task_failed"};},
                result=&mut server_write_task=>{break match result {Ok(Err(reason))=>reason,_=>"server_writer_stopped"};},
                result=&mut stdout_task,if !stdout_done=>{
                    stdout_done=true;
                    match result {Ok(Ok(()))=>{stdout_complete=true;
                        if let Ok(Ok(status))=timeout(Duration::from_millis(200),child.wait()).await {child_exit=status.code();child_reaped=true;break "child_exited";}
                        break "server_stdout_eof";
                    },Ok(Err(reason))=>break reason,Err(_)=>break "stdout_task_failed"}
                },
                result=&mut stderr_task,if !stderr_done=>{stderr_done=true;stderr_complete=matches!(result,Ok(Ok(())));if !stderr_complete {break "stderr_task_failed";}},
                _=&mut writer_task=>{break "parent_output_failed";},
                _=tokio::time::sleep_until(deadline)=>{break "timed_out";},
                _=monitor.tick()=>{if parent_closed() {break "parent_eof";}},
            }
        };
        // Stop further server execution, then collect already bounded stream tails.
        if !child_reaped {let _=child.start_kill();let _=timeout(Duration::from_millis(200),child.wait()).await;}
        if !stdout_done {stdout_complete=matches!(timeout(Duration::from_millis(200),&mut stdout_task).await,Ok(Ok(Ok(()))));}
        if !stderr_done {stderr_complete=matches!(timeout(Duration::from_millis(200),&mut stderr_task).await,Ok(Ok(Ok(()))));}
        input_task.abort();server_write_task.abort();stdout_task.abort();stderr_task.abort();
        let (ack_tx,ack_rx)=oneshot::channel();
        let event=Event {value:json!({"kind":"terminal","reason":reason,"child_exit_code":child_exit,
            "stdout_complete":stdout_complete,"stderr_complete":stderr_complete,"process_tree_stopped":Value::Null}),_permit:None,written:Some(ack_tx)};
        if timeout(Duration::from_millis(200),events_tx.send(event)).await.is_ok() {
            let _=timeout(Duration::from_millis(200),ack_rx).await;
        }
        // Parent also kills this exact, unreaped group before reaping the helper.
        // This is termination, not a claimed terminal receipt for managed work.
        terminate_group(pgid)
    }
    pub(super) fn run(session_id:&str)->! {
        if !valid_session(session_id) {eprintln!("invalid LSP transport session ID");std::process::exit(2);}
        let pgid=match owned_group() {Ok(value)=>value,Err(_)=>{eprintln!("LSP helper requires its own session/process group");std::process::exit(2);}};
        std::panic::set_hook(Box::new(move |_|terminate_group(pgid)));
        let runtime=match tokio::runtime::Builder::new_multi_thread().worker_threads(2).max_blocking_threads(4).enable_all().build() {
            Ok(value)=>value,Err(_)=>{eprintln!("LSP async runtime unavailable");std::process::exit(2);}
        };
        let _=runtime.block_on(session(session_id,pgid));
        terminate_group(pgid)
    }

    #[cfg(test)]
    mod tests {
        use super::*;
        #[tokio::test]
        async fn fragmented_lsp_bytes_and_strict_headers() {
            let body="{\"jsonrpc\":\"2.0\",\"id\":\"雪\",\"result\":null}";
            let frame=format!("Content-Length: {}\r\n\r\n{}",body.len(),body);
            let (mut writer,reader)=tokio::io::duplex(16);
            let task=tokio::spawn(async move {for byte in frame.bytes() {writer.write_all(&[byte]).await.unwrap();}});
            let got=lsp_frame(&mut BufReader::new(reader)).await.unwrap().unwrap();assert_eq!(got.0,body);task.await.unwrap();
            for bad in [b"Content-Length: 1\n\nX".as_slice(),b"Content-Length: 1\r\ncontent-length: 1\r\n\r\nx",b"Content-Length: 999999999\r\n\r\n",b"Content-Length: 0\r\n\r\n"] {
                assert!(lsp_frame(&mut BufReader::new(bad)).await.is_err());
            }
        }
        #[test]
        fn rpc_ids_versions_duplicate_keys_and_mixed_shapes() {
            for good in [br#"{"jsonrpc":"2.0","id":"configuration","method":"workspace/configuration"}"#.as_slice(),br#"{"jsonrpc":"2.0","id":1,"result":null}"#,br#"{"jsonrpc":"2.0","method":"$/progress","params":{}}"#] {assert!(rpc(good).is_ok());}
            for bad in [br#"{"jsonrpc":"2.0","id":true,"result":null}"#.as_slice(),br#"{"jsonrpc":"2.0","id":1.0,"result":null}"#,br#"{"jsonrpc":"2.0","id":1,"id":1,"result":null}"#,br#"{"jsonrpc":"2.0","id":1,"result":null,"method":"x"}"#] {assert!(rpc(bad).is_err());}
        }
    }
}
