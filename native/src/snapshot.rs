//! Snapshot bytes and metadata using the Python manifest's exact representation.
use rayon::prelude::*;
use serde_json::{json, Map, Value};
use sha2::{Digest, Sha256};
use std::collections::BTreeSet;
use std::ffi::OsStr;
use std::fs::{self, File, Metadata, OpenOptions};
use std::io::{Read, Seek, SeekFrom};
use std::path::{Component, Path, PathBuf};
use std::process::{Command, Stdio};
use std::thread;
use std::time::{Duration, Instant};

const MAX_NAMES: usize = 1_000_000;
const MAX_NAME_BYTES: usize = 64 * 1024 * 1024;
const MAX_GIT_BYTES: u64 = 100 * 1024 * 1024;

fn check_time(deadline: Instant) -> Result<(), String> {
    if Instant::now() >= deadline {
        Err("native source scan deadline exceeded".into())
    } else {
        Ok(())
    }
}

fn text(value: &OsStr) -> Result<String, String> {
    value
        .to_str()
        .map(str::to_owned)
        .ok_or_else(|| "source path is not valid Unicode".into())
}

fn trim(value: &str) -> &str {
    value.trim_matches(|c: char| c.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&c))
}

fn bytes_text(value: &[u8]) -> Result<&str, String> {
    std::str::from_utf8(value).map_err(|_| "Git output is not valid Unicode".into())
}

fn git_path_record(value: &str) -> &str {
    let value = value.strip_suffix('\n').unwrap_or(value);
    #[cfg(windows)]
    let value = value.strip_suffix('\r').unwrap_or(value);
    value
}

fn shown_path(path: &Path) -> Result<String, String> {
    let value = text(path.as_os_str())?;
    #[cfg(windows)]
    {
        if let Some(value) = value.strip_prefix(r"\\?\UNC\") {
            return Ok(format!(r"\\{value}"));
        }
        if let Some(value) = value.strip_prefix(r"\\?\") {
            return Ok(value.to_owned());
        }
    }
    Ok(value)
}

fn resolve(path: &Path) -> Result<PathBuf, String> {
    let absolute = if path.is_absolute() {
        path.to_owned()
    } else {
        std::env::current_dir()
            .map_err(|e| e.to_string())?
            .join(path)
    };
    if let Ok(resolved) = fs::canonicalize(&absolute) {
        return Ok(resolved);
    }
    let mut resolved = PathBuf::new();
    for part in absolute.components() {
        match part {
            Component::ParentDir => {
                resolved.pop();
            }
            Component::CurDir => {}
            _ => {
                resolved.push(part.as_os_str());
                if let Ok(found) = fs::canonicalize(&resolved) {
                    resolved = found;
                }
            }
        }
    }
    Ok(resolved)
}

/// Compare the Git-reported top against the workspace without changing its
/// lexical binding. Windows canonicalize returns a verbatim path, while the
/// snapshot root intentionally retains the caller's ordinary absolute spelling.
fn git_root_matches(top: &[u8], root: &Path) -> Result<bool, String> {
    let top = resolve(Path::new(git_path_record(bytes_text(top)?)))?;
    #[cfg(windows)]
    {
        // Match pathlib's Windows case semantics after giving both operands the
        // same canonical/prefix representation. Do not trim filename whitespace.
        Ok(shown_path(&top)?.to_lowercase() == shown_path(&resolve(root)?)?.to_lowercase())
    }
    #[cfg(not(windows))]
    {
        Ok(top == root)
    }
}

/// Python fnmatchcase semantics: '*' crosses '/', backslash is a literal.
#[derive(Clone, Debug)]
enum Token {
    Star,
    Any,
    Literal(char),
    Class(bool, Vec<(char, char)>),
}

fn character_class(chars: &[char]) -> Token {
    // Keep literal hyphens separate from range separators. Python fnmatch
    // normalizes descending ranges before interpreting the leading '!'.
    // Removing a range can expose a new leading negation character.
    let mut chunks = Vec::<Vec<char>>::new();
    let mut start = 0;
    let mut search = if chars.first() == Some(&'!') { 2 } else { 1 };
    while search < chars.len() {
        let Some(offset) = chars[search..].iter().position(|c| *c == '-') else {
            break;
        };
        let dash = search + offset;
        chunks.push(chars[start..dash].to_vec());
        start = dash + 1;
        search = dash + 3;
    }
    if start < chars.len() {
        chunks.push(chars[start..].to_vec());
    } else if let Some(last) = chunks.last_mut() {
        last.push('-');
    }
    for index in (1..chunks.len()).rev() {
        if let (Some(left), Some(right)) = (chunks[index - 1].last(), chunks[index].first()) {
            if left > right {
                let right = chunks.remove(index);
                chunks[index - 1].pop();
                chunks[index - 1].extend(right.into_iter().skip(1));
            }
        }
    }
    let mut atoms = Vec::new();
    for (index, chunk) in chunks.iter().enumerate() {
        if index != 0 {
            atoms.push(('-', true));
        }
        atoms.extend(chunk.iter().map(|c| (*c, false)));
    }
    let negated = atoms.first() == Some(&('!', false));
    let mut cursor = usize::from(negated);
    let mut ranges = Vec::new();
    while cursor < atoms.len() {
        if cursor + 2 < atoms.len()
            && !atoms[cursor].1
            && atoms[cursor + 1].1
            && !atoms[cursor + 2].1
        {
            ranges.push((atoms[cursor].0, atoms[cursor + 2].0));
            cursor += 3;
        } else {
            ranges.push((atoms[cursor].0, atoms[cursor].0));
            cursor += 1;
        }
    }
    Token::Class(negated, ranges)
}

#[derive(Clone, Debug)]
struct Pattern {
    tokens: Vec<Token>,
    directory: Option<String>,
}
impl Pattern {
    fn new(pattern: &str) -> Self {
        let chars: Vec<char> = pattern.chars().collect();
        let mut tokens = Vec::new();
        let mut i = 0;
        while i < chars.len() {
            let character = chars[i];
            i += 1;
            match character {
                '*' => {
                    if !matches!(tokens.last(), Some(Token::Star)) {
                        tokens.push(Token::Star);
                    }
                }
                '?' => tokens.push(Token::Any),
                '[' => {
                    let start = i;
                    let mut end = i;
                    if chars.get(end) == Some(&'!') {
                        end += 1;
                    }
                    if chars.get(end) == Some(&']') {
                        end += 1;
                    }
                    while end < chars.len() && chars[end] != ']' {
                        end += 1;
                    }
                    if end == chars.len() {
                        tokens.push(Token::Literal('['));
                        continue;
                    }
                    tokens.push(character_class(&chars[start..end]));
                    i = end + 1;
                }
                other => tokens.push(Token::Literal(other)),
            }
        }
        Self {
            tokens,
            directory: pattern.strip_suffix("/**").map(str::to_owned),
        }
    }

    fn matches(&self, path: &str) -> bool {
        if self.directory.as_deref() == Some(path) {
            return true;
        }
        let chars: Vec<char> = path.chars().collect();
        let mut prior = vec![false; chars.len() + 1];
        prior[0] = true;
        for token in &self.tokens {
            let mut next = vec![false; chars.len() + 1];
            if matches!(token, Token::Star) {
                next[0] = prior[0];
            }
            for (j, character) in chars.iter().enumerate() {
                next[j + 1] = match token {
                    Token::Star => prior[j + 1] || next[j],
                    Token::Any => prior[j],
                    Token::Literal(value) => prior[j] && character == value,
                    Token::Class(negated, ranges) => {
                        prior[j]
                            && (*negated
                                != ranges.iter().any(|(a, b)| a <= character && character <= b))
                    }
                };
            }
            prior = next;
        }
        prior[chars.len()]
    }
}

fn excluded(path: &str, patterns: &[Pattern]) -> bool {
    patterns.iter().any(|pattern| pattern.matches(path))
}

fn git(root: &Path, args: &[&str], deadline: Instant) -> Result<Option<Vec<u8>>, String> {
    check_time(deadline)?;
    let deadline = deadline.min(Instant::now() + Duration::from_secs(30));
    // File capture cannot block on a descendant inheriting a pipe after Git exits.
    let mut stdout = tempfile::tempfile().map_err(|e| format!("cannot create Git capture: {e}"))?;
    let capture = stdout.try_clone().map_err(|e| e.to_string())?;
    let mut child = match Command::new("git")
        .arg("-C")
        .arg(root)
        .args(args)
        .stdin(Stdio::null())
        .stdout(capture)
        .stderr(Stdio::null())
        .spawn()
    {
        Ok(child) => child,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(None),
        Err(error) => return Err(format!("cannot execute Git: {error}")),
    };
    let mut overflow = false;
    let started = Instant::now();
    let status = loop {
        match child.try_wait() {
            Ok(Some(status)) => break Some(status),
            Ok(None) => {}
            Err(error) => {
                let _ = child.kill();
                let _ = child.wait();
                return Err(format!("cannot wait for Git: {error}"));
            }
        }
        overflow = stdout.metadata().map_err(|e| e.to_string())?.len() > MAX_GIT_BYTES;
        if Instant::now() >= deadline || overflow {
            let _ = child.kill();
            let _ = child.wait();
            break None;
        }
        // Bound long waits without a coarse fixed delay on ordinary short Git
        // inventory commands. No signal handler, sockets or background service.
        thread::sleep(if started.elapsed() < Duration::from_millis(10) {
            Duration::from_micros(200)
        } else {
            Duration::from_millis(2)
        });
    };
    stdout.seek(SeekFrom::Start(0)).map_err(|e| e.to_string())?;
    let mut output = Vec::new();
    stdout
        .take(MAX_GIT_BYTES + 1)
        .read_to_end(&mut output)
        .map_err(|e| format!("cannot read Git output: {e}"))?;
    if overflow || output.len() as u64 > MAX_GIT_BYTES {
        return Err("Git output exceeds 100 MiB".into());
    }
    // Match the Python adapter: failed/missing/timed-out Git is unavailable.
    Ok(status.filter(|status| status.success()).map(|_| output))
}

pub fn git_head(root: &Path) -> Result<Option<String>, String> {
    git_head_until(root, Instant::now() + Duration::from_secs(30))
}
pub fn git_head_until(root: &Path, deadline: Instant) -> Result<Option<String>, String> {
    git(root, &["rev-parse", "HEAD"], deadline)?
        .map(|value| Ok(trim(bytes_text(&value)?).to_owned()))
        .transpose()
}

#[derive(Default)]
struct Names {
    values: BTreeSet<String>,
    bytes: usize,
}
impl Names {
    fn insert(&mut self, name: String) -> Result<(), String> {
        if !self.values.contains(&name) {
            self.bytes = self.bytes.saturating_add(name.len());
            if self.values.len() >= MAX_NAMES || self.bytes > MAX_NAME_BYTES {
                return Err(
                    "source enumeration exceeds 1,000,000 names or 64 MiB of path names".into(),
                );
            }
            self.values.insert(name);
        }
        Ok(())
    }
}

fn relative(path: &Path, root: &Path) -> Result<String, String> {
    let value = text(
        path.strip_prefix(root)
            .map_err(|_| "source path escaped workspace")?
            .as_os_str(),
    )?;
    #[cfg(windows)]
    {
        return Ok(value.replace('\\', "/"));
    }
    #[cfg(not(windows))]
    {
        Ok(value)
    }
}

fn walk(
    root: &Path,
    start: &Path,
    patterns: &[Pattern],
    names: &mut Names,
    submodule: bool,
    deadline: Instant,
) -> Result<(), String> {
    let mut stack = vec![start.to_owned()];
    while let Some(directory) = stack.pop() {
        check_time(deadline)?;
        let entries = match fs::read_dir(&directory) {
            Ok(entries) => entries,
            Err(error)
                if matches!(
                    error.kind(),
                    std::io::ErrorKind::NotFound
                        | std::io::ErrorKind::PermissionDenied
                        | std::io::ErrorKind::NotADirectory
                ) =>
            {
                continue
            }
            Err(error) => return Err(format!("cannot enumerate {}: {error}", directory.display())),
        };
        for entry in entries {
            check_time(deadline)?;
            let entry = entry.map_err(|e| format!("cannot enumerate source: {e}"))?;
            let path = entry.path();
            let name = relative(&path, root)?;
            let kind = entry
                .file_type()
                .map_err(|e| format!("cannot inspect {}: {e}", path.display()))?;
            if submodule && entry.file_name() == ".git" {
                continue;
            }
            if kind.is_symlink() {
                if !submodule || !path.is_dir() {
                    names.insert(name)?;
                }
            } else if kind.is_dir() {
                if submodule || !excluded(&name, patterns) {
                    stack.push(path);
                }
            } else {
                names.insert(name)?;
            }
        }
    }
    Ok(())
}

fn ignored_walk(
    root: &Path,
    patterns: &[Pattern],
    names: &mut Names,
    deadline: Instant,
) -> Result<(), String> {
    let filter_root = root.to_owned();
    let filter_patterns = patterns.to_owned();
    let mut builder = ignore::WalkBuilder::new(root);
    builder
        .hidden(false)
        .ignore(false)
        .require_git(false)
        .follow_links(false)
        .filter_entry(move |entry| {
            relative(entry.path(), &filter_root)
                .map(|path| path.is_empty() || !excluded(&path, &filter_patterns))
                .unwrap_or(false)
        });
    for entry in builder.build() {
        check_time(deadline)?;
        let entry = entry.map_err(|e| format!("cannot enumerate source: {e}"))?;
        if entry.depth() == 0 || entry.file_type().is_some_and(|kind| kind.is_dir()) {
            continue;
        }
        names.insert(relative(entry.path(), root)?)?;
    }
    Ok(())
}

struct Inventory {
    names: Vec<String>,
    git: Option<Value>,
    is_git: bool,
}
fn inventory(
    root: &Path,
    excludes: &[String],
    metadata: bool,
    respect_ignore: bool,
    deadline: Instant,
) -> Result<Inventory, String> {
    let patterns: Vec<Pattern> = excludes.iter().map(|value| Pattern::new(value)).collect();
    let head = if metadata {
        git_head_until(root, deadline)?
    } else {
        None
    };
    let top = git(root, &["rev-parse", "--show-toplevel"], deadline)?;
    let is_git = match top {
        Some(top) => git_root_matches(&top, root)?,
        None => false,
    };
    let mut names = Names::default();
    let mut metadata_value = None;
    if is_git {
        let files = git(
            root,
            &[
                "ls-files",
                "--cached",
                "--others",
                "--exclude-standard",
                "-z",
            ],
            deadline,
        )?
        .ok_or("required Git file inventory unavailable")?;
        for name in files.split(|b| *b == 0).filter(|name| !name.is_empty()) {
            names.insert(bytes_text(name)?.to_owned())?;
        }
        if metadata {
            let mut settings = Map::new();
            for key in ["core.autocrlf", "core.eol", "core.filemode"] {
                settings.insert(key.into(), Value::Null);
            }
            for entry in git(
                root,
                &[
                    "config",
                    "--null",
                    "--get-regexp",
                    r"^(core\.autocrlf|core\.eol|core\.filemode)$",
                ],
                deadline,
            )?
            .unwrap_or_default()
            .split(|b| *b == 0)
            {
                let parts: Vec<&[u8]> = entry.splitn(2, |b| *b == b'\n').collect();
                let name = bytes_text(parts[0])?.to_lowercase();
                if settings.contains_key(&name) {
                    settings.insert(
                        name,
                        Value::String(
                            trim(bytes_text(parts.get(1).copied().unwrap_or_default())?).to_owned(),
                        ),
                    );
                }
            }
            let branch = git(root, &["symbolic-ref", "--short", "HEAD"], deadline)?
                .map(|raw| Ok::<_, String>(trim(bytes_text(&raw)?).to_owned()))
                .transpose()?
                .filter(|value| !value.is_empty());
            metadata_value = Some(json!({"head":head,"branch":branch,"settings":settings}));
        }
        let stages = git(root, &["ls-files", "--stage", "-z"], deadline)?
            .ok_or("required Git stage inventory unavailable")?;
        for stage in stages.split(|b| *b == 0) {
            if stage.starts_with(b"160000 ") {
                let tab = stage
                    .iter()
                    .position(|b| *b == b'\t')
                    .ok_or("invalid Git stage output")?;
                let name = bytes_text(&stage[tab + 1..])?;
                let path = root.join(name);
                if path.is_dir() {
                    walk(root, &path, &patterns, &mut names, true, deadline)?;
                }
            }
        }
    } else if respect_ignore {
        ignored_walk(root, &patterns, &mut names, deadline)?;
    } else {
        walk(root, root, &patterns, &mut names, false, deadline)?;
    }
    Ok(Inventory {
        names: names
            .values
            .into_iter()
            .filter(|name| !excluded(name, &patterns))
            .collect(),
        git: metadata_value,
        is_git,
    })
}

pub fn source_paths(root: &Path, excludes: &[String]) -> Result<Vec<String>, String> {
    source_paths_until(root, excludes, Instant::now() + Duration::from_secs(30))
}
pub fn source_paths_until(
    root: &Path,
    excludes: &[String],
    deadline: Instant,
) -> Result<Vec<String>, String> {
    let root = resolve(root)?;
    let mut filters = vec![".git".into(), ".git/**".into()];
    filters.extend_from_slice(excludes);
    Ok(inventory(&root, &filters, false, true, deadline)?.names)
}

#[derive(Debug, PartialEq)]
struct Stamp {
    device: u64,
    inode: u64,
    mode: u32,
    size: u64,
    modified: i128,
    changed: i128,
}

#[cfg(unix)]
fn stamp(metadata: &Metadata, _file: Option<&File>) -> Result<Stamp, String> {
    use std::os::unix::fs::MetadataExt;
    Ok(Stamp {
        device: metadata.dev(),
        inode: metadata.ino(),
        mode: metadata.mode(),
        size: metadata.size(),
        modified: metadata.mtime() as i128 * 1_000_000_000 + metadata.mtime_nsec() as i128,
        changed: metadata.ctime() as i128 * 1_000_000_000 + metadata.ctime_nsec() as i128,
    })
}

#[cfg(windows)]
fn stamp(metadata: &Metadata, file: Option<&File>) -> Result<Stamp, String> {
    use std::os::windows::{fs::MetadataExt, io::AsRawHandle};
    use windows_sys::Win32::Storage::FileSystem::{
        FileBasicInfo, GetFileInformationByHandle, GetFileInformationByHandleEx,
        BY_HANDLE_FILE_INFORMATION, FILE_BASIC_INFO,
    };
    let mut info: BY_HANDLE_FILE_INFORMATION = unsafe { std::mem::zeroed() };
    let file = file.ok_or("missing Windows metadata handle")?;
    if unsafe { GetFileInformationByHandle(file.as_raw_handle(), &mut info) } == 0 {
        return Err(std::io::Error::last_os_error().to_string());
    }
    let mut basic: FILE_BASIC_INFO = unsafe { std::mem::zeroed() };
    if unsafe {
        GetFileInformationByHandleEx(
            file.as_raw_handle(),
            FileBasicInfo,
            (&mut basic as *mut FILE_BASIC_INFO).cast(),
            std::mem::size_of::<FILE_BASIC_INFO>() as u32,
        )
    } == 0
    {
        return Err(std::io::Error::last_os_error().to_string());
    }
    Ok(Stamp {
        device: info.dwVolumeSerialNumber as u64,
        inode: (info.nFileIndexHigh as u64) << 32 | info.nFileIndexLow as u64,
        mode: basic.FileAttributes,
        size: metadata.file_size(),
        modified: basic.LastWriteTime as i128,
        changed: basic.ChangeTime as i128,
    })
}

fn observed(path: &Path) -> Result<(Metadata, Stamp), std::io::Error> {
    #[cfg(unix)]
    let metadata = fs::symlink_metadata(path)?;
    #[cfg(unix)]
    let identity = stamp(&metadata, None);
    #[cfg(windows)]
    let (metadata, identity) = {
        use std::os::windows::fs::OpenOptionsExt;
        use windows_sys::Win32::Storage::FileSystem::{
            FILE_FLAG_BACKUP_SEMANTICS, FILE_FLAG_OPEN_REPARSE_POINT,
        };
        let file = OpenOptions::new()
            .access_mode(0)
            .custom_flags(FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT)
            .open(path)?;
        let metadata = file.metadata()?;
        let identity = stamp(&metadata, Some(&file));
        (metadata, identity)
    };
    Ok((metadata, identity.map_err(std::io::Error::other)?))
}

#[cfg(any(windows, test))]
fn windows_root_identity(volume: u64, identifier: [u8; 16]) -> Value {
    let file_id: String = identifier
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect();
    json!({"identity_kind":"windows-file-id-info-v1",
           "device":format!("{volume:016x}"),"inode":file_id})
}

fn root_identity(path: &Path) -> Result<Value, String> {
    #[cfg(unix)]
    {
        let (_, identity) = observed(path).map_err(|e| e.to_string())?;
        Ok(json!({"device":identity.device,"inode":identity.inode}))
    }
    #[cfg(windows)]
    {
        use std::os::windows::{
            fs::{MetadataExt, OpenOptionsExt},
            io::AsRawHandle,
        };
        use windows_sys::Win32::Storage::FileSystem::{
            FileIdInfo, GetFileInformationByHandleEx, FILE_FLAG_BACKUP_SEMANTICS,
            FILE_FLAG_OPEN_REPARSE_POINT, FILE_ID_INFO, FILE_SHARE_READ, FILE_SHARE_WRITE,
        };
        let file = OpenOptions::new()
            .access_mode(0)
            .share_mode(FILE_SHARE_READ | FILE_SHARE_WRITE)
            .custom_flags(FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT)
            .open(path)
            .map_err(|e| e.to_string())?;
        let metadata = file.metadata().map_err(|e| e.to_string())?;
        if !metadata.is_dir() || metadata.file_attributes() & 0x400 != 0 {
            return Err("physical source root must be an ordinary directory".into());
        }
        let mut value: FILE_ID_INFO = unsafe { std::mem::zeroed() };
        if unsafe {
            GetFileInformationByHandleEx(
                file.as_raw_handle(),
                FileIdInfo,
                (&mut value as *mut FILE_ID_INFO).cast(),
                std::mem::size_of::<FILE_ID_INFO>() as u32,
            )
        } == 0
        {
            return Err("full Windows FileIdInfo root identity is unavailable".into());
        }
        Ok(windows_root_identity(
            value.VolumeSerialNumber,
            value.FileId.Identifier,
        ))
    }
}

fn parent_topology(path: &Path) -> Result<Vec<(PathBuf, u64, u64)>, String> {
    let mut result = Vec::new();
    for parent in path.ancestors().skip(1) {
        let (metadata, identity) = match observed(parent) {
            Ok(value) => value,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => continue,
            Err(error) => return Err(error.to_string()),
        };
        #[cfg(windows)]
        let reparse = {
            use std::os::windows::fs::MetadataExt;
            metadata.file_attributes() & 0x400 != 0
        };
        #[cfg(unix)]
        let reparse = false;
        if metadata.file_type().is_symlink() || reparse {
            return Err(format!(
                "source ancestor is symlink/reparse: {}",
                parent.display()
            ));
        }
        if !metadata.is_dir() {
            return Err(format!(
                "source ancestor is not a directory: {}",
                parent.display()
            ));
        }
        result.push((parent.to_owned(), identity.device, identity.inode));
    }
    Ok(result)
}

fn lexical_absolute(path: &Path) -> Result<PathBuf, String> {
    let absolute = if path.is_absolute() {
        path.to_owned()
    } else {
        std::env::current_dir()
            .map_err(|e| e.to_string())?
            .join(path)
    };
    let mut result = PathBuf::new();
    for part in absolute.components() {
        match part {
            Component::ParentDir => {
                result.pop();
            }
            Component::CurDir => {}
            _ => result.push(part.as_os_str()),
        }
    }
    Ok(result)
}

fn regular_open(path: &Path) -> Result<File, String> {
    let mut options = OpenOptions::new();
    options.read(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.custom_flags(libc::O_NONBLOCK | libc::O_NOFOLLOW);
    }
    #[cfg(windows)]
    {
        use std::os::windows::fs::OpenOptionsExt;
        use windows_sys::Win32::Storage::FileSystem::FILE_FLAG_OPEN_REPARSE_POINT;
        options.custom_flags(FILE_FLAG_OPEN_REPARSE_POINT);
    }
    let file = options
        .open(path)
        .map_err(|e| format!("cannot open {}: {e}", path.display()))?;
    if !file.metadata().map_err(|e| e.to_string())?.is_file() {
        return Err(format!(
            "read input must be a regular file: {}",
            path.display()
        ));
    }
    Ok(file)
}

#[cfg(unix)]
fn rooted_open(path: &Path) -> Result<File, String> {
    use std::ffi::CString;
    use std::os::unix::{
        ffi::OsStrExt,
        io::{AsRawFd, FromRawFd},
    };
    if !path.is_absolute() {
        return Err("regular source path must be absolute".into());
    }
    let parts: Vec<_> = path
        .components()
        .filter(|part| !matches!(part, Component::RootDir | Component::CurDir))
        .collect();
    if parts.is_empty()
        || parts
            .iter()
            .any(|part| !matches!(part, Component::Normal(_)))
    {
        return Err("regular source path must be normalized".into());
    }
    #[cfg(target_os = "linux")]
    let directory_flags = libc::O_PATH | libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC;
    #[cfg(not(target_os = "linux"))]
    let directory_flags =
        libc::O_RDONLY | libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC | libc::O_NONBLOCK;
    let descriptor = unsafe { libc::open(c"/".as_ptr(), directory_flags) };
    if descriptor < 0 {
        return Err(std::io::Error::last_os_error().to_string());
    }
    let mut parent = unsafe { File::from_raw_fd(descriptor) };
    for (index, part) in parts.iter().enumerate() {
        let name =
            CString::new(part.as_os_str().as_bytes()).map_err(|_| "source path contains NUL")?;
        let flags = if index + 1 == parts.len() {
            libc::O_RDONLY | libc::O_NOFOLLOW | libc::O_NONBLOCK | libc::O_CLOEXEC
        } else {
            directory_flags
        };
        let descriptor = unsafe { libc::openat(parent.as_raw_fd(), name.as_ptr(), flags) };
        if descriptor < 0 {
            return Err(format!(
                "cannot safely open {}: {}",
                path.display(),
                std::io::Error::last_os_error()
            ));
        }
        parent = unsafe { File::from_raw_fd(descriptor) };
    }
    Ok(parent)
}

#[cfg(windows)]
fn rooted_open(path: &Path) -> Result<File, String> {
    use std::os::windows::{fs::MetadataExt, io::AsRawHandle};
    use windows_sys::Win32::Storage::FileSystem::{
        GetFinalPathNameByHandleW, FILE_ATTRIBUTE_REPARSE_POINT,
    };
    if !path.is_absolute() {
        return Err("regular source path must be absolute".into());
    }
    for parent in path.ancestors() {
        if fs::symlink_metadata(parent)
            .map_err(|e| e.to_string())?
            .file_attributes()
            & FILE_ATTRIBUTE_REPARSE_POINT
            != 0
        {
            return Err("source path crosses a reparse point".into());
        }
    }
    let file = regular_open(path)?;
    let needed =
        unsafe { GetFinalPathNameByHandleW(file.as_raw_handle(), std::ptr::null_mut(), 0, 0) };
    if needed == 0 {
        return Err(std::io::Error::last_os_error().to_string());
    }
    let mut buffer = vec![0u16; needed as usize + 1];
    let length = unsafe {
        GetFinalPathNameByHandleW(
            file.as_raw_handle(),
            buffer.as_mut_ptr(),
            buffer.len() as u32,
            0,
        )
    };
    if length == 0 || length as usize >= buffer.len() {
        return Err("cannot resolve opened source handle".into());
    }
    let actual = PathBuf::from(
        String::from_utf16(&buffer[..length as usize]).map_err(|_| "invalid source handle path")?,
    );
    if shown_path(&actual)?.to_lowercase() != shown_path(path)?.to_lowercase() {
        return Err("opened source escaped requested path".into());
    }
    Ok(file)
}

pub fn read_regular(path: &Path, limit: u64) -> Result<Vec<u8>, String> {
    let mut file = rooted_open(path)?;
    let metadata = file.metadata().map_err(|e| e.to_string())?;
    if !metadata.is_file() {
        return Err("source input must be a regular file".into());
    }
    if metadata.len() > limit {
        return Err(format!("source input exceeds {limit} bytes"));
    }
    let before = stamp(&metadata, Some(&file))?;
    let mut data = Vec::new();
    (&mut file)
        .take(limit.saturating_add(1))
        .read_to_end(&mut data)
        .map_err(|e| e.to_string())?;
    if data.len() as u64 > limit {
        return Err(format!("source input exceeds {limit} bytes"));
    }
    let after = stamp(&file.metadata().map_err(|e| e.to_string())?, Some(&file))?;
    if before != after || observed(path).map_err(|e| e.to_string())?.1 != after {
        return Err("source changed during read".into());
    }
    Ok(data)
}

fn entry(path: &Path, deadline: Instant) -> Result<Value, String> {
    check_time(deadline)?;
    let topology = parent_topology(path)?;
    let (metadata, before) = match observed(path) {
        Ok(value) => value,
        Err(error)
            if matches!(
                error.kind(),
                std::io::ErrorKind::NotFound | std::io::ErrorKind::NotADirectory
            ) =>
        {
            return Ok(json!({"kind":"missing"}))
        }
        Err(error) => return Err(format!("cannot inspect {}: {error}", path.display())),
    };
    let result = if metadata.file_type().is_symlink() {
        let target = text(fs::read_link(path).map_err(|e| e.to_string())?.as_os_str())?;
        json!({"kind":"symlink","sha256":format!("{:x}",Sha256::digest(target.as_bytes())),"target":target})
    } else if metadata.is_file() {
        let mut file = rooted_open(path)?;
        if stamp(&file.metadata().map_err(|e| e.to_string())?, Some(&file))? != before {
            return Err(format!(
                "source changed during snapshot: {}",
                path.display()
            ));
        }
        let mut hash = Sha256::new();
        let mut buffer = vec![0u8; 1024 * 1024];
        loop {
            check_time(deadline)?;
            let count = file.read(&mut buffer).map_err(|e| e.to_string())?;
            if count == 0 {
                break;
            }
            hash.update(&buffer[..count]);
        }
        #[cfg(unix)]
        let executable = {
            use std::os::unix::fs::MetadataExt;
            json!(metadata.mode() & 0o111 != 0)
        };
        #[cfg(windows)]
        let executable = Value::Null;
        json!({"kind":"file","sha256":format!("{:x}",hash.finalize()),"size":metadata.len(),"executable":executable})
    } else {
        return Ok(json!({"kind":"directory"}));
    };
    if parent_topology(path)? != topology || observed(path).map_err(|e| e.to_string())?.1 != before
    {
        return Err(format!(
            "source changed during snapshot: {}",
            path.display()
        ));
    }
    Ok(result)
}

fn canonical(value: &Value, output: &mut Vec<u8>) -> Result<(), String> {
    match value {
        Value::Object(values) => {
            output.push(b'{');
            let mut keys: Vec<_> = values.keys().collect();
            keys.sort();
            for (index, key) in keys.iter().enumerate() {
                if index != 0 {
                    output.push(b',');
                }
                serde_json::to_writer(&mut *output, key).map_err(|e| e.to_string())?;
                output.push(b':');
                canonical(&values[*key], output)?;
            }
            output.push(b'}');
        }
        Value::Array(values) => {
            output.push(b'[');
            for (index, value) in values.iter().enumerate() {
                if index != 0 {
                    output.push(b',');
                }
                canonical(value, output)?;
            }
            output.push(b']');
        }
        _ => serde_json::to_writer(&mut *output, value).map_err(|e| e.to_string())?,
    }
    Ok(())
}

pub fn snapshot(contract: &Value) -> Result<Value, String> {
    if !contract.is_object() {
        return Err("snapshot contract must be an object".into());
    }
    let deadline = Instant::now() + Duration::from_secs(120);
    let mut result = json!({"schema_version":1,"kind":"non_file","files":{},"external_inputs":{}});
    let mut bound_root = None;
    if let Some(cwd) = contract
        .get("cwd")
        .and_then(Value::as_str)
        .filter(|value| !value.is_empty())
    {
        let root = lexical_absolute(Path::new(cwd))?;
        let topology = parent_topology(&root.join(".harness-root-binding"))?;
        let root_binding = root_identity(&root)?;
        result["root_binding"] = root_binding.clone();
        bound_root = Some((root.clone(), root_binding.clone()));
        let mut excludes = vec![".git".to_owned(), ".git/**".to_owned()];
        if let Some(values) = contract.pointer("/scope/exclude") {
            for value in values.as_array().ok_or("scope.exclude must be an array")? {
                excludes.push(
                    value
                        .as_str()
                        .ok_or("scope.exclude entries must be strings")?
                        .to_owned(),
                );
            }
        }
        let inventory = inventory(
            &root,
            &excludes,
            true,
            false,
            deadline.min(Instant::now() + Duration::from_secs(30)),
        )?;
        result["kind"] = json!(if inventory.is_git { "git" } else { "directory" });
        result["cwd"] = json!(shown_path(&root)?);
        result["exclude"] = json!(excludes);
        if let Some(metadata) = inventory.git {
            result["git"] = metadata;
        }
        let workers = thread::available_parallelism()
            .map(usize::from)
            .unwrap_or(1)
            .min(8);
        let pool = rayon::ThreadPoolBuilder::new()
            .num_threads(workers)
            .build()
            .map_err(|e| format!("cannot create source workers: {e}"))?;
        let rows: Vec<(String, Value)> = pool.install(|| {
            inventory
                .names
                .par_iter()
                .map(|name| entry(&root.join(name), deadline).map(|value| (name.clone(), value)))
                .collect::<Result<_, _>>()
        })?;
        result["files"] = Value::Object(rows.into_iter().collect());
        if parent_topology(&root.join(".harness-root-binding"))? != topology
            || root_identity(&root)? != root_binding
        {
            return Err("source root physical identity changed during snapshot".into());
        }
    }
    if let Some(inputs) = contract.get("inputs") {
        for item in inputs.as_array().ok_or("inputs must be an array")? {
            let path = lexical_absolute(Path::new(
                item.get("path")
                    .and_then(Value::as_str)
                    .ok_or("input requires path")?,
            ))?;
            if !path.is_file() {
                return Err(format!("Required input missing: {}", path.display()));
            }
            let observed = entry(&path, deadline)?;
            if let Some(expected) = item
                .get("sha256")
                .filter(|value| !value.is_null() && value.as_str() != Some(""))
            {
                if observed.get("sha256") != Some(expected) {
                    return Err(format!("Frozen input changed: {}", path.display()));
                }
            }
            result["external_inputs"][shown_path(&path)?] = observed;
        }
    }
    if let Some((root, binding)) = bound_root {
        if root_identity(&root)? != binding {
            return Err("source root physical identity changed before manifest publication".into());
        }
    }
    result["snapshot_scheme_version"] = json!(2);
    result["topology_policy"] = json!("nofollow-ancestors-literal-leaf-v1");
    let mut content = result.clone();
    content.as_object_mut().unwrap().remove("root_binding");
    let mut content_encoded = Vec::new();
    canonical(&content, &mut content_encoded)?;
    result["content_digest"] = json!(format!("{:x}", Sha256::digest(&content_encoded)));
    let mut encoded = Vec::new();
    canonical(&result, &mut encoded)?;
    result["digest"] = json!(format!("{:x}", Sha256::digest(&encoded)));
    Ok(result)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn windows_root_identity_retains_all_bits_as_opaque_bytes() {
        let mut identifier = [0u8; 16];
        for (index, byte) in identifier.iter_mut().enumerate() {
            *byte = index as u8;
        }
        let result = windows_root_identity(0x123456789abcdef0, identifier);
        assert_eq!(
            result,
            json!({"identity_kind":"windows-file-id-info-v1",
            "device":"123456789abcdef0","inode":"000102030405060708090a0b0c0d0e0f"})
        );
        let mut changed = identifier;
        changed[15] ^= 1;
        assert_ne!(result, windows_root_identity(0x123456789abcdef0, changed));
        assert_ne!(result, windows_root_identity(0x9abcdef0, identifier));
    }

    #[test]
    fn glob_matches_python_path_rules() {
        for (pattern, path, expected) in [
            ("a/**", "a", true),
            ("a/*", "a/b/c", true),
            ("[!a-c]?.rs", "z1.rs", true),
            ("[z-a]", "a", false),
            ("[!z-a]", "a", true),
            ("[]]", "]", true),
            ("[[]", "[", true),
            (r"a\b", r"a\b", true),
            ("?.py", "猫.py", true),
            ("*.py", "a\nb.py", true),
            ("[!]", "[!]", true),
        ] {
            assert_eq!(
                Pattern::new(pattern).matches(path),
                expected,
                "{pattern:?} {path:?}"
            );
        }
        // Oracle cases from Python fnmatchcase. Descending-range removal can
        // expose a negation; hyphens inside chunks remain literal characters.
        let alphabet = "!-/0AZ^_`abz~猫";
        for (pattern, accepted) in [
            ("[a--!a]", "!-/0AZ^_`bz~猫"),
            ("[a--!]", alphabet),
            ("[z-a!b]", "!-/0AZ^_`az~猫"),
            ("[!a--]", alphabet),
            ("[a-b-c]", "-ab"),
            ("[a--b]", "b"),
            ("[--a]", "-/0AZ^_`a"),
            ("[a---z]", "-z"),
            ("[!a--!z]", "-/0AZ^_`ab~猫"),
            ("[z-a-b]", "-b"),
        ] {
            for value in alphabet.chars() {
                assert_eq!(
                    Pattern::new(pattern).matches(&value.to_string()),
                    accepted.contains(value),
                    "{pattern:?} {value:?}"
                );
            }
        }
    }

    #[test]
    fn git_path_output_keeps_filename_whitespace() {
        assert_eq!(git_path_record("/project \n"), "/project ");
        assert_eq!(git_path_record("/project\n\n"), "/project\n");
        #[cfg(unix)]
        assert_eq!(git_path_record("/project\r\n"), "/project\r");
        #[cfg(windows)]
        assert_eq!(git_path_record("C:/project\r\n"), "C:/project");
    }

    #[cfg(windows)]
    #[test]
    fn git_root_comparison_normalizes_verbatim_prefix_and_drive_case() {
        let temporary = tempfile::tempdir().unwrap();
        let root = temporary.path().join("Проект with spaces");
        fs::create_dir(&root).unwrap();
        let ordinary = shown_path(&fs::canonicalize(&root).unwrap()).unwrap();
        let verbatim = fs::canonicalize(&root).unwrap();
        let top = format!("{}\n", ordinary.replace('\\', "/"));
        assert_ne!(
            resolve(Path::new(git_path_record(&top))).unwrap(),
            Path::new(&ordinary)
        );
        for spelling in [
            PathBuf::from(&ordinary),
            verbatim,
            PathBuf::from(ordinary.to_lowercase()),
        ] {
            assert!(git_root_matches(top.as_bytes(), &spelling).unwrap());
        }
        let child = root.join("nested");
        fs::create_dir(&child).unwrap();
        assert!(!git_root_matches(top.as_bytes(), &child).unwrap());
        assert!(!git_root_matches(top.as_bytes(), temporary.path()).unwrap());
    }

    #[test]
    fn git_inventory_keeps_tracked_deletion_and_rejects_subdirectory_root() {
        let temporary = tempfile::tempdir().unwrap();
        let root = PathBuf::from(shown_path(&fs::canonicalize(temporary.path()).unwrap()).unwrap());
        let deadline = Instant::now() + Duration::from_secs(30);
        // No commit or user identity is required to qualify the cached inventory.
        assert!(git(&root, &["init", "-q"], deadline).unwrap().is_some());
        fs::write(root.join("deleted.txt"), b"tracked bytes").unwrap();
        assert!(git(&root, &["add", "deleted.txt"], deadline)
            .unwrap()
            .is_some());
        fs::remove_file(root.join("deleted.txt")).unwrap();
        let inventory = inventory(&root, &[], true, false, deadline).unwrap();
        assert!(inventory.is_git);
        assert!(inventory.git.is_some());
        assert!(inventory.names.contains(&"deleted.txt".to_owned()));
        let nested = root.join("nested");
        fs::create_dir(&nested).unwrap();
        let inventory = super::inventory(&nested, &[], true, false, deadline).unwrap();
        assert!(!inventory.is_git);
        assert!(inventory.git.is_none());
    }

    #[test]
    fn canonical_unicode_has_python_order_and_escaping() {
        let mut output = Vec::new();
        canonical(&json!({"猫":"line\n\u{1}","é":false,"a":null}), &mut output).unwrap();
        assert_eq!(
            String::from_utf8(output).unwrap(),
            "{\"a\":null,\"é\":false,\"猫\":\"line\\n\\u0001\"}"
        );
    }

    #[test]
    fn non_file_snapshot_has_content_digest() {
        let value = snapshot(&json!({})).unwrap();
        assert_eq!(value["kind"], "non_file");
        let mut body = value.clone();
        body.as_object_mut().unwrap().remove("digest");
        let mut encoded = Vec::new();
        canonical(&body, &mut encoded).unwrap();
        assert_eq!(value["digest"], format!("{:x}", Sha256::digest(encoded)));
    }

    #[test]
    fn regular_reader_keeps_bytes_and_rejects_oversize() {
        let temporary = tempfile::tempdir().unwrap();
        let root = fs::canonicalize(temporary.path()).unwrap();
        let file = root.join("source.txt");
        fs::write(&file, b"source\r\nbytes").unwrap();
        assert_eq!(read_regular(&file, 64).unwrap(), b"source\r\nbytes");
        assert!(read_regular(&file, 3).unwrap_err().contains("exceeds"));
        assert!(read_regular(&root, 64).is_err());
    }

    #[cfg(unix)]
    #[test]
    fn regular_reader_refuses_fifo_and_symlink_ancestors() {
        use std::os::unix::{ffi::OsStrExt, fs::symlink};
        let temporary = tempfile::tempdir().unwrap();
        let root = fs::canonicalize(temporary.path()).unwrap();
        let actual = root.join("actual");
        fs::create_dir(&actual).unwrap();
        fs::write(actual.join("source.txt"), b"source").unwrap();
        symlink(&actual, root.join("alias")).unwrap();
        symlink(actual.join("source.txt"), root.join("link.txt")).unwrap();
        assert!(read_regular(&root.join("alias/source.txt"), 64).is_err());
        assert!(read_regular(&root.join("link.txt"), 64).is_err());
        let fifo = root.join("pipe");
        let name = std::ffi::CString::new(fifo.as_os_str().as_bytes()).unwrap();
        assert_eq!(unsafe { libc::mkfifo(name.as_ptr(), 0o600) }, 0);
        assert!(read_regular(&fifo, 64).is_err());
        assert_eq!(
            entry(&fifo, Instant::now() + Duration::from_secs(1)).unwrap(),
            json!({"kind":"directory"})
        );
    }

    #[test]
    fn repomap_inventory_honors_gitignore_without_git() {
        let temporary = tempfile::tempdir().unwrap();
        let root = fs::canonicalize(temporary.path()).unwrap();
        fs::write(root.join(".gitignore"), "ignored.rs\n").unwrap();
        fs::write(root.join("source.rs"), "fn source() {}\n").unwrap();
        fs::write(root.join("ignored.rs"), "fn ignored() {}\n").unwrap();
        let files = source_paths(&root, &[]).unwrap();
        assert!(files.contains(&"source.rs".into()));
        assert!(!files.contains(&"ignored.rs".into()));
        let legacy = snapshot(&json!({"cwd":shown_path(&root).unwrap()})).unwrap();
        assert!(legacy["files"].get("ignored.rs").is_some());
        assert!(source_paths_until(&root, &[], Instant::now()).is_err());
    }
}
