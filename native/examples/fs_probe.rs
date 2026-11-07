//! Development conformance probe for the real public read-only library.
//! This is not an advertised native wire operation or a Python consumer switch.
use harness_native::fs_read::{self,Control};
use serde_json::json;
use sha2::{Digest,Sha256};
use std::path::Path;
use std::sync::atomic::AtomicBool;
use std::time::{Duration,Instant};

fn main() {
    let args:Vec<_>=std::env::args_os().skip(1).collect();
    if args.len()<2 || args.len()>3 {eprintln!("usage: fs_probe read|hash|resources PATH [LIMIT]");std::process::exit(2);}
    let path=Path::new(&args[1]);let deadline=Instant::now()+Duration::from_secs(5);
    let result=match args[0].to_str() {
        Some("read")=>{
            let limit=args.get(2).and_then(|v|v.to_str()).and_then(|v|v.parse::<u64>().ok()).unwrap_or(16*1024*1024);
            fs_read::read_bounded_until(path,limit,deadline).map(|data|json!({"status":"complete","size":data.len(),"sha256":format!("{:x}",Sha256::digest(&data))}))
        },
        Some("hash")=>fs_read::hash_regular_until(path,deadline).map(|data|json!({"status":"complete","size":data.size,"sha256":data.sha256})),
        #[cfg(target_os="linux")]
        Some("resources")=>{
            let count=||std::fs::read_dir("/proc/self/fd").map(|rows|rows.count());
            let before=count().unwrap();let flag=AtomicBool::new(true);
            for _ in 0..100 {
                assert!(fs_read::read_bounded_until(path,0,deadline).is_err());
                assert!(fs_read::read_bounded_until(path,16*1024*1024,deadline).is_ok());
                assert!(fs_read::hash_regular(path,Control {deadline,cancelled:Some(&flag)}).is_err());
                assert!(fs_read::hash_regular_until(path,Instant::now()).is_err());
            }
            let after=count().unwrap();assert_eq!(before,after);
            Ok(json!({"status":"complete","before_fds":before,"after_fds":after,"iterations":100}))
        },
        _=>{eprintln!("unsupported probe operation");std::process::exit(2);},
    };
    match result {
        Ok(value)=>println!("{value}"),
        Err(error)=>{println!("{}",json!({"status":"refused","kind":format!("{:?}",error.kind)}));std::process::exit(1);},
    }
}
