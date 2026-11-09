//! Read-only Linux file mechanisms with owned handles and cooperative mutation checks.
//!
//! These are not an OS sandbox or transaction isolation against an arbitrary writer.
//! Non-Linux platforms retain their existing snapshot/RepoMap implementation; these
//! new operations explicitly refuse unsupported platforms until separately qualified.
use std::fmt;
use std::path::Path;
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::Instant;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ErrorKind {
    InvalidInput, UnsupportedPlatform, UnsafeTopology, NotRegularFile,
    TooLarge, Changed, Timeout, Cancelled, NotFound, Io,
}
#[derive(Debug)]
pub struct Error { pub kind: ErrorKind, message: &'static str }
impl fmt::Display for Error {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result { f.write_str(self.message) }
}
impl std::error::Error for Error {}
impl Error {
    fn new(kind: ErrorKind, message: &'static str) -> Self { Self {kind,message} }
}
impl From<std::io::Error> for Error {
    fn from(value: std::io::Error) -> Self {
        let kind = if value.kind() == std::io::ErrorKind::NotFound {ErrorKind::NotFound} else {ErrorKind::Io};
        Self::new(kind,"Native ordinary-file IO failed")
    }
}

#[derive(Clone, Copy)]
pub struct Control<'a> { pub deadline: Instant, pub cancelled: Option<&'a AtomicBool> }
impl Control<'_> {
    fn check(&self) -> Result<(), Error> {
        if self.cancelled.is_some_and(|flag| flag.load(Ordering::Relaxed)) {
            return Err(Error::new(ErrorKind::Cancelled,"Native file operation cancelled"));
        }
        if Instant::now() >= self.deadline {
            return Err(Error::new(ErrorKind::Timeout,"Native file operation deadline exceeded"));
        }
        Ok(())
    }
}
#[derive(Debug)]
pub struct HashedFile {
    pub sha256: String, pub size: u64, pub executable: bool,
    pub device: u64, pub inode: u64,
}

pub fn read_bounded_until(path: &Path, limit: u64, deadline: Instant) -> Result<Vec<u8>, Error> {
    read_bounded(path,limit,Control {deadline,cancelled:None})
}
pub fn hash_regular_until(path: &Path, deadline: Instant) -> Result<HashedFile, Error> {
    hash_regular(path,Control {deadline,cancelled:None})
}
#[cfg(target_os="linux")]
pub fn read_bounded(path: &Path, limit: u64, control: Control<'_>) -> Result<Vec<u8>, Error> {
    linux::read(path,limit,control,&mut |_| {})
}
#[cfg(target_os="linux")]
pub fn hash_regular(path: &Path, control: Control<'_>) -> Result<HashedFile, Error> {
    linux::hash(path,control,&mut |_| {})
}
#[cfg(not(target_os="linux"))]
pub fn read_bounded(_path: &Path, _limit: u64, _control: Control<'_>) -> Result<Vec<u8>, Error> {
    Err(Error::new(ErrorKind::UnsupportedPlatform,"Strict native file reads are not qualified on this platform"))
}
#[cfg(not(target_os="linux"))]
pub fn hash_regular(_path: &Path, _control: Control<'_>) -> Result<HashedFile, Error> {
    Err(Error::new(ErrorKind::UnsupportedPlatform,"Strict native streamed hashing is not qualified on this platform"))
}

#[cfg(target_os="linux")]
mod linux {
    use super::*;
    use sha2::{Digest,Sha256};
    use std::ffi::CString;
    use std::fs::{self,File,Metadata};
    use std::io::{Read,Seek,SeekFrom};
    use std::os::unix::{ffi::OsStrExt,fs::MetadataExt,io::{AsRawFd,FromRawFd}};
    use std::path::{Component,PathBuf};

    const BLOCK: usize = 65_536;
    // The native bounded-read contract matches Python's initial 16 MiB cap.
    // Streaming hashes do not inherit this cap.
    const MAX_BOUNDED_READ: u64 = 16 * 1024 * 1024;
    const DIR_FLAGS: i32 = libc::O_PATH | libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC;
    #[derive(Clone,Debug,Eq,PartialEq)]
    struct Stamp {dev:u64,ino:u64,mode:u32,size:u64,mtime:(i64,i64),ctime:(i64,i64),uid:u32,gid:u32,nlink:u64}
    fn stamp(m:&Metadata) -> Stamp {
        Stamp {dev:m.dev(),ino:m.ino(),mode:m.mode(),size:m.size(),mtime:(m.mtime(),m.mtime_nsec()),
            ctime:(m.ctime(),m.ctime_nsec()),uid:m.uid(),gid:m.gid(),nlink:m.nlink()}
    }
    #[derive(Clone,Copy,Debug,Eq,PartialEq)]
    pub(super) enum Phase {AfterParentsPinned,BeforeLeafOpen,AfterOpen,AfterFirstRead,AfterHashBlock,BeforeFinalCheck}
    struct Directory {path:PathBuf,file:File,identity:(u64,u64)}
    struct AnchoredFile {path:PathBuf,parents:Vec<Directory>,file:File,before:Stamp}
    fn regular(m:&Metadata) -> Result<(),Error> {
        if !m.file_type().is_file() {return Err(Error::new(ErrorKind::NotRegularFile,"Native input must be an ordinary file"));}
        Ok(())
    }
    fn changed() -> Error {Error::new(ErrorKind::Changed,"Native source identity or metadata changed")}
    fn unsafe_path() -> Error {Error::new(ErrorKind::UnsafeTopology,"Native source path crosses an unsafe ancestor")}
    fn check_spelling(path:&Path) -> Result<(),Error> {
        let raw=path.as_os_str().as_bytes();
        if !path.is_absolute() || path.to_str().is_none() || raw.starts_with(b"//")
            || raw.len()>65_536 || raw.ends_with(b"/") || raw.contains(&0)
            || raw.split(|b|*b==b'/').any(|part|part==b"." || part==b"..") {
            return Err(Error::new(ErrorKind::InvalidInput,"Native source path must have a supported absolute spelling"));
        }
        Ok(())
    }
    fn open_at(parent:&File,name:&CString,flags:i32) -> Result<File,Error> {
        // SAFETY: parent is a live owned directory descriptor; CString has a
        // terminating NUL and no interior NUL. A successful new fd transfers
        // exactly once to File; failure does not construct an owned handle.
        let fd=unsafe {libc::openat(parent.as_raw_fd(),name.as_ptr(),flags)};
        if fd<0 {
            let error=std::io::Error::last_os_error();
            if matches!(error.raw_os_error(),Some(libc::ELOOP) | Some(libc::ENOTDIR)) {return Err(unsafe_path());}
            return Err(error.into());
        }
        Ok(unsafe {File::from_raw_fd(fd)})
    }
    impl AnchoredFile {
        fn open<F:FnMut(Phase)>(path:&Path,control:Control<'_>,hook:&mut F) -> Result<Self,Error> {
            control.check()?;check_spelling(path)?;
            let parts:Vec<_>=path.components().filter_map(|part|match part {Component::Normal(v)=>Some(v),_=>None}).collect();
            if parts.is_empty() || parts.len()>4096 {return Err(Error::new(ErrorKind::InvalidInput,"Native path depth exceeded"));}
            // SAFETY: constant slash C string, no borrowed fd; ownership moves
            // to File exactly once following a successful open.
            let fd=unsafe {libc::open(c"/".as_ptr(),DIR_FLAGS)};
            if fd<0 {return Err(std::io::Error::last_os_error().into());}
            let root=unsafe {File::from_raw_fd(fd)};
            let meta=root.metadata()?;
            let mut parents=vec![Directory {path:PathBuf::from("/"),identity:(meta.dev(),meta.ino()),file:root}];
            for part in &parts[..parts.len()-1] {
                control.check()?;
                let parent=parents.last().ok_or_else(unsafe_path)?;
                let next_path=parent.path.join(part);
                let before=fs::symlink_metadata(&next_path)?;
                if !before.file_type().is_dir() {return Err(unsafe_path());}
                let name=CString::new(part.as_bytes()).map_err(|_|unsafe_path())?;
                let file=open_at(&parent.file,&name,DIR_FLAGS)?;
                let opened=file.metadata()?;
                if !opened.is_dir() || (before.dev(),before.ino())!=(opened.dev(),opened.ino()) {return Err(changed());}
                parents.push(Directory {path:next_path,file,identity:(opened.dev(),opened.ino())});
            }
            hook(Phase::AfterParentsPinned);Self::check_parents(&parents,control)?;
            let before=fs::symlink_metadata(path)?;regular(&before)?;
            hook(Phase::BeforeLeafOpen);control.check()?;
            let name=CString::new(parts[parts.len()-1].as_bytes()).map_err(|_|unsafe_path())?;
            let parent=parents.last().ok_or_else(unsafe_path)?;
            let file=open_at(&parent.file,&name,libc::O_RDONLY|libc::O_NOFOLLOW|libc::O_NONBLOCK|libc::O_CLOEXEC)?;
            let opened=file.metadata()?;regular(&opened)?;
            if stamp(&before)!=stamp(&opened) {return Err(changed());}
            let value=Self {path:path.to_owned(),parents,file,before:stamp(&opened)};
            value.check(control)?;
            Ok(value)
        }
        fn check_parents(parents:&[Directory],control:Control<'_>) -> Result<(),Error> {
            for parent in parents {
                control.check()?;
                let path=fs::symlink_metadata(&parent.path).map_err(|_|changed())?;
                let descriptor=parent.file.metadata()?;
                if !path.file_type().is_dir() || !descriptor.is_dir()
                    || (path.dev(),path.ino())!=parent.identity
                    || (descriptor.dev(),descriptor.ino())!=parent.identity {return Err(changed());}
            }
            Ok(())
        }
        fn check(&self,control:Control<'_>) -> Result<(),Error> {
            Self::check_parents(&self.parents,control)?;
            let path=fs::symlink_metadata(&self.path).map_err(|_|changed())?;regular(&path)?;
            let descriptor=self.file.metadata()?;regular(&descriptor)?;
            if stamp(&path)!=self.before || stamp(&descriptor)!=self.before {return Err(changed());}
            Ok(())
        }
    }
    pub(super) fn read<F:FnMut(Phase)>(path:&Path,limit:u64,control:Control<'_>,hook:&mut F) -> Result<Vec<u8>,Error> {
        control.check()?;
        if limit>MAX_BOUNDED_READ {return Err(Error::new(ErrorKind::InvalidInput,"Native bounded read limit exceeds 16 MiB"));}
        let mut input=AnchoredFile::open(path,control,hook)?;hook(Phase::AfterOpen);
        if input.before.size>limit {return Err(Error::new(ErrorKind::TooLarge,"Native source exceeds bounded read limit"));}
        let mut data=Vec::with_capacity(input.before.size as usize);
        let mut block=[0u8;BLOCK];
        while data.len() as u64<=limit {
            control.check()?;
            let wanted=BLOCK.min((limit+1-data.len() as u64) as usize);
            let count=input.file.read(&mut block[..wanted])?;
            control.check()?;
            if count==0 {break;}
            data.extend_from_slice(&block[..count]);
        }
        if data.len() as u64>limit {return Err(Error::new(ErrorKind::TooLarge,"Native source exceeds bounded read limit"));}
        let after_first=stamp(&input.file.metadata()?);
        hook(Phase::AfterFirstRead);
        input.file.seek(SeekFrom::Start(0))?;
        let mut offset=0usize;
        loop {
            control.check()?;
            let wanted=BLOCK.min((limit+1-offset as u64) as usize);
            if wanted==0 {return Err(changed());}
            let count=input.file.read(&mut block[..wanted])?;
            control.check()?;
            if count==0 {break;}
            if offset+count>data.len() || data[offset..offset+count]!=block[..count] {
                return Err(Error::new(ErrorKind::Changed,"Native file content changed between bounded passes"));
            }
            offset+=count;
        }
        if offset!=data.len() || after_first!=input.before {return Err(changed());}
        hook(Phase::BeforeFinalCheck);input.check(control)?;
        Ok(data)
    }
    pub(super) fn hash<F:FnMut(Phase)>(path:&Path,control:Control<'_>,hook:&mut F) -> Result<HashedFile,Error> {
        let mut input=AnchoredFile::open(path,control,hook)?;hook(Phase::AfterOpen);
        let mut hash=Sha256::new();let mut block=[0u8;BLOCK];let mut size=0u64;
        loop {
            control.check()?;let count=input.file.read(&mut block)?;control.check()?;
            if count==0 {break;}
            size=size.checked_add(count as u64).ok_or_else(changed)?;
            hash.update(&block[..count]);hook(Phase::AfterHashBlock);
        }
        hook(Phase::BeforeFinalCheck);input.check(control)?;
        if size!=input.before.size {return Err(changed());}
        Ok(HashedFile {sha256:format!("{:x}",hash.finalize()),size,executable:input.before.mode&0o111!=0,
            device:input.before.dev,inode:input.before.ino})
    }

    #[cfg(test)]
    mod tests {
        use super::*;
        use std::os::unix::fs::{symlink,PermissionsExt};
        use std::time::Duration;
        fn control()->Control<'static> {Control {deadline:Instant::now()+Duration::from_secs(5),cancelled:None}}
        fn fixture()->(tempfile::TempDir,PathBuf) {
            let temp=tempfile::tempdir().unwrap();let path=temp.path().join("parent/data");
            fs::create_dir(path.parent().unwrap()).unwrap();fs::write(&path,b"original").unwrap();(temp,path)
        }
        #[test]
        fn ordinary_bytes_limits_unicode_and_streamed_hash() {
            let (_temp,path)=fixture();
            for data in [b"".as_slice(),b"a\r\nb\n",b"\xef\xbb\xbfA=1", "я🙂\u{2028}b\u{85}".as_bytes()] {
                fs::write(&path,data).unwrap();
                assert_eq!(read(&path,data.len() as u64,control(),&mut |_|{}).unwrap(),data);
                let h=hash(&path,control(),&mut |_|{}).unwrap();
                assert_eq!(h.sha256,format!("{:x}",Sha256::digest(data)));assert_eq!(h.size,data.len() as u64);
                if !data.is_empty() {assert_eq!(read(&path,data.len() as u64-1,control(),&mut |_|{}).unwrap_err().kind,ErrorKind::TooLarge);}
            }
            let name=path.parent().unwrap().join("back\\slash\n雪 ");fs::write(&name,b"literal").unwrap();
            assert_eq!(read(&name,7,control(),&mut |_|{}).unwrap(),b"literal");
            fs::write(&path,vec![b'x';(MAX_BOUNDED_READ+1) as usize]).unwrap();
            assert_eq!(read(&path,MAX_BOUNDED_READ,control(),&mut |_|{}).unwrap_err().kind,ErrorKind::TooLarge);
            assert_eq!(hash(&path,control(),&mut |_|{}).unwrap().size,MAX_BOUNDED_READ+1);
        }
        #[test]
        fn symlink_ancestor_fifo_device_and_unsupported_spelling_are_refused() {
            let (temp,path)=fixture();let link=temp.path().join("link");symlink(&path,&link).unwrap();
            assert_eq!(read(&link,64,control(),&mut |_|{}).unwrap_err().kind,ErrorKind::NotRegularFile);
            let alias=temp.path().join("alias");symlink(path.parent().unwrap(),&alias).unwrap();
            assert!(read(&alias.join("data"),64,control(),&mut |_|{}).is_err());
            let fifo=temp.path().join("fifo");let c=CString::new(fifo.as_os_str().as_bytes()).unwrap();
            assert_eq!(unsafe {libc::mkfifo(c.as_ptr(),0o600)},0);
            // Only the filesystem type matters to this read API. Create an
            // inert socket inode; no listener, connection, or IPC is required.
            let socket=temp.path().join("socket");let c=CString::new(socket.as_os_str().as_bytes()).unwrap();
            assert_eq!(unsafe {libc::mknod(c.as_ptr(),libc::S_IFSOCK|0o600,0)},0);
            for p in [&fifo,&socket,Path::new("/dev/null"),path.parent().unwrap()] {
                assert_eq!(read(p,64,control(),&mut |_|{}).unwrap_err().kind,ErrorKind::NotRegularFile);
            }
            for p in [PathBuf::from(format!("/{}",path.display())),path.parent().unwrap().join("../parent/data"),PathBuf::from("relative")] {
                assert_eq!(read(&p,64,control(),&mut |_|{}).unwrap_err().kind,ErrorKind::InvalidInput);
            }
        }
        #[test]
        fn second_pass_detects_same_size_write_with_restored_mtime() {
            let (_temp,path)=fixture();let before=fs::metadata(&path).unwrap();
            let error=read(&path,8,control(),&mut |phase| if phase==Phase::AfterFirstRead {
                fs::write(&path,b"modified").unwrap();
                let file=File::open(&path).unwrap();
                file.set_times(std::fs::FileTimes::new().set_modified(before.modified().unwrap())).unwrap();
            }).unwrap_err();
            assert_eq!(error.kind,ErrorKind::Changed);assert!(error.message.contains("content changed"));
        }
        #[test]
        fn raced_fifo_and_ancestor_swap_before_leaf_open_are_refused() {
            let (temp,path)=fixture();
            let e=read(&path,8,control(),&mut |phase| if phase==Phase::BeforeLeafOpen {
                fs::remove_file(&path).unwrap();let name=CString::new(path.as_os_str().as_bytes()).unwrap();
                assert_eq!(unsafe {libc::mkfifo(name.as_ptr(),0o600)},0);
            }).unwrap_err();
            assert_eq!(e.kind,ErrorKind::NotRegularFile);
            fs::remove_file(&path).unwrap();fs::write(&path,b"original").unwrap();
            let e=read(&path,8,control(),&mut |phase| if phase==Phase::AfterParentsPinned {
                let old=temp.path().join("old");fs::rename(path.parent().unwrap(),&old).unwrap();
                symlink(old,path.parent().unwrap()).unwrap();
            }).unwrap_err();assert_eq!(e.kind,ErrorKind::Changed);
        }
        #[test]
        fn actual_read_enforces_cap_plus_one_after_metadata_inspection() {
            let (_temp,path)=fixture();
            let e=read(&path,8,control(),&mut |phase| if phase==Phase::AfterOpen {
                let mut f=fs::OpenOptions::new().append(true).open(&path).unwrap();
                std::io::Write::write_all(&mut f,b"x").unwrap();
            }).unwrap_err();assert_eq!(e.kind,ErrorKind::TooLarge);
        }
        #[test]
        fn replacement_delete_recreate_and_ancestor_swap_are_detected() {
            for scenario in 0..4 {
                let (temp,path)=fixture();let mut changed_once=false;
                let alternate=temp.path().join("replacement");
                if scenario==2 {
                    fs::create_dir(&alternate).unwrap();
                    // Both names already refer to the same leaf before open:
                    // replacing only the ancestor changes no leaf stamp.
                    fs::hard_link(&path,alternate.join("data")).unwrap();
                }
                let error=read(&path,8,control(),&mut |phase| if phase==Phase::AfterOpen && !changed_once {
                    changed_once=true;
                    match scenario {
                        0=>{fs::remove_file(&path).unwrap();fs::write(&path,b"original").unwrap();},
                        1=>{let other=temp.path().join("other");fs::write(&other,b"original").unwrap();fs::rename(other,&path).unwrap();},
                        2=>{let old=temp.path().join("old");fs::rename(path.parent().unwrap(),&old).unwrap();fs::rename(&alternate,path.parent().unwrap()).unwrap();},
                        _=>{fs::set_permissions(&path,fs::Permissions::from_mode(0o400)).unwrap();},
                    }
                }).unwrap_err();
                assert_eq!(error.kind,ErrorKind::Changed,"scenario {scenario}");
            }
        }
        #[test]
        fn streaming_hash_observes_midstream_mutation_and_bounded_cancellation() {
            let (_temp,path)=fixture();fs::write(&path,vec![b'a';BLOCK*3]).unwrap();let mut changed_once=false;
            let e=hash(&path,control(),&mut |phase| if phase==Phase::AfterHashBlock && !changed_once {
                changed_once=true;let mut f=fs::OpenOptions::new().write(true).open(&path).unwrap();
                std::io::Write::write_all(&mut f,b"b").unwrap();
            }).unwrap_err();assert_eq!(e.kind,ErrorKind::Changed);
            let flag=AtomicBool::new(false);let c=Control {deadline:control().deadline,cancelled:Some(&flag)};
            let e=hash(&path,c,&mut |phase| if phase==Phase::AfterHashBlock {flag.store(true,Ordering::Relaxed)}).unwrap_err();
            assert_eq!(e.kind,ErrorKind::Cancelled);
            flag.store(false,Ordering::Relaxed);
            let e=read(&path,(BLOCK*3) as u64,c,&mut |phase| if phase==Phase::AfterFirstRead {flag.store(true,Ordering::Relaxed)}).unwrap_err();
            assert_eq!(e.kind,ErrorKind::Cancelled);
            let c=Control {deadline:Instant::now(),cancelled:None};assert_eq!(hash(&path,c,&mut |_|{}).unwrap_err().kind,ErrorKind::Timeout);
        }
    }
}
