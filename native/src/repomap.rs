//! Bounded, ephemeral syntactic repository map. No database or persistent index.
//!
//! Aider's pinned RepoMap motivated the definition/reference file graph and
//! personalized PageRank. This is an independent implementation; no Aider code,
//! SQLite/cache, lexer fallback or TreeContext rendering is copied.
//! https://github.com/Aider-AI/aider/blob/5dc9490bb35f9729ef2c95d00a19ccd30c26339c/aider/repomap.py

use crate::snapshot::{git_head_until, read_regular, source_paths_until};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::collections::{BTreeMap, BTreeSet, HashSet};
use std::fs;
use std::path::{Component, Path, PathBuf};
use std::time::{Duration, Instant};
use tree_sitter::{Language, Node, ParseOptions, Parser};

const MAX_DEFINITIONS: usize = 25_000;
const MAX_REFERENCES: usize = 100_000;
const MAX_EDGES: usize = 200_000;
const MAX_NODES_PER_FILE: usize = 250_000;
const MAX_NODES: usize = 5_000_000;
const SCORE_SCALE: f64 = 1_000_000.0;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum Lang {
    Python,
    Javascript,
    Typescript,
    Tsx,
    Rust,
    Go,
    C,
    Cpp,
}

impl Lang {
    fn for_path(path: &str) -> Option<Self> {
        match Path::new(path)
            .extension()?
            .to_str()?
            .to_ascii_lowercase()
            .as_str()
        {
            "py" | "pyi" => Some(Self::Python),
            "js" | "jsx" | "mjs" | "cjs" => Some(Self::Javascript),
            "ts" | "mts" | "cts" => Some(Self::Typescript),
            "tsx" => Some(Self::Tsx),
            "rs" => Some(Self::Rust),
            "go" => Some(Self::Go),
            "c" => Some(Self::C),
            "h" | "cc" | "cpp" | "cxx" | "hpp" | "hh" | "hxx" => Some(Self::Cpp),
            _ => None,
        }
    }
    fn name(self) -> &'static str {
        match self {
            Self::Python => "python",
            Self::Javascript => "javascript",
            Self::Typescript => "typescript",
            Self::Tsx => "tsx",
            Self::Rust => "rust",
            Self::Go => "go",
            Self::C => "c",
            Self::Cpp => "cpp",
        }
    }
    fn family(self) -> &'static str {
        match self {
            Self::Javascript | Self::Typescript | Self::Tsx => "js_ts",
            Self::C | Self::Cpp => "c_cpp",
            other => other.name(),
        }
    }
    fn language(self) -> Language {
        match self {
            Self::Python => tree_sitter_python::LANGUAGE.into(),
            Self::Javascript => tree_sitter_javascript::LANGUAGE.into(),
            Self::Typescript => tree_sitter_typescript::LANGUAGE_TYPESCRIPT.into(),
            Self::Tsx => tree_sitter_typescript::LANGUAGE_TSX.into(),
            Self::Rust => tree_sitter_rust::LANGUAGE.into(),
            Self::Go => tree_sitter_go::LANGUAGE.into(),
            Self::C => tree_sitter_c::LANGUAGE.into(),
            Self::Cpp => tree_sitter_cpp::LANGUAGE.into(),
        }
    }
}

#[derive(Clone, Debug)]
struct Definition {
    name: String,
    kind: &'static str,
    line: usize,
    end_line: usize,
    signature: String,
    signature_truncated: bool,
}
#[derive(Clone, Debug)]
struct Reference {
    name: String,
    line: usize,
}
struct File {
    path: String,
    sha: String,
    language: Lang,
    definitions: Vec<Definition>,
    references: Vec<Reference>,
    syntax_errors: bool,
}
struct Parsed {
    definitions: Vec<Definition>,
    references: Vec<Reference>,
    syntax_errors: bool,
    partial: bool,
    nodes: usize,
}

#[derive(Default)]
struct Scan {
    counts: BTreeMap<String, usize>,
    reasons: BTreeSet<String>,
    issues: Vec<Value>,
    omitted_issues: usize,
    unsupported: BTreeMap<String, usize>,
    unsupported_other: usize,
    bytes: usize,
    nodes: usize,
}
impl Scan {
    fn issue(&mut self, path: &str, reason: &str) {
        *self.counts.entry(reason.to_owned()).or_default() += 1;
        self.reasons.insert(reason.to_owned());
        if self.issues.len() < 8 {
            self.issues
                .push(json!({"path": clip(path, 240), "reason": reason}));
        } else {
            self.omitted_issues += 1;
        }
    }
}

fn hex(data: &[u8]) -> String {
    format!("{:x}", Sha256::digest(data))
}
fn clip(value: &str, count: usize) -> String {
    value.chars().take(count).collect()
}
fn integer(request: &Value, key: &str, default: u64, min: u64, max: u64) -> Result<usize, String> {
    let value = match request.get(key) {
        None => default,
        Some(value) => value
            .as_u64()
            .ok_or_else(|| format!("{key} must be an integer"))?,
    };
    if value < min || value > max {
        return Err(format!("{key} must be {min}..{max}"));
    }
    Ok(value as usize)
}
fn strings(request: &Value, key: &str, count: usize, length: usize) -> Result<Vec<String>, String> {
    let Some(value) = request.get(key) else {
        return Ok(Vec::new());
    };
    let list = value
        .as_array()
        .ok_or_else(|| format!("{key} must be an array"))?;
    if list.len() > count {
        return Err(format!("Too many {key}"));
    }
    list.iter()
        .map(|item| {
            let text = item
                .as_str()
                .ok_or_else(|| format!("{key} needs strings"))?;
            if text.is_empty() || text.len() > length || text.contains('\0') {
                return Err(format!("Invalid {key} item"));
            }
            Ok(text.to_owned())
        })
        .collect()
}

fn allowed_path(name: &str) -> bool {
    if name.is_empty() || name.len() > 4096 || name.contains(['\\', ':', '\0']) {
        return false;
    }
    let parts: Vec<_> = name.split('/').collect();
    if parts
        .iter()
        .any(|part| part.is_empty() || *part == "." || *part == "..")
    {
        return false;
    }
    if Path::new(name)
        .components()
        .any(|part| !matches!(part, Component::Normal(_)))
    {
        return false;
    }
    if parts.iter().any(|part| {
        matches!(
            part.to_ascii_lowercase().as_str(),
            ".git" | ".ssh" | ".aws" | ".venv" | "node_modules" | "__pycache__"
        )
    }) {
        return false;
    }
    let last = parts.last().unwrap().to_ascii_lowercase();
    if last.starts_with(".env")
        || matches!(
            last.as_str(),
            "auth.json"
                | "credentials"
                | "credentials.json"
                | "cookies.json"
                | "id_rsa"
                | "id_ed25519"
        )
    {
        return false;
    }
    !matches!(
        Path::new(&last).extension().and_then(|ext| ext.to_str()),
        Some("pem" | "key" | "p12" | "pfx")
    )
}

fn guarded_path(root: &Path, name: &str) -> Result<PathBuf, String> {
    if !allowed_path(name) {
        return Err("denied_path".to_owned());
    }
    let mut path = root.to_path_buf();
    for part in name.split('/') {
        path.push(part);
        let metadata = fs::symlink_metadata(&path).map_err(|_| "unavailable_file".to_owned())?;
        if metadata.file_type().is_symlink() {
            return Err("symlink".to_owned());
        }
        #[cfg(windows)]
        {
            use std::os::windows::fs::MetadataExt;
            if metadata.file_attributes() & 0x400 != 0 {
                return Err("reparse_point".to_owned());
            }
        }
    }
    if !fs::symlink_metadata(&path)
        .map_err(|_| "unavailable_file".to_owned())?
        .is_file()
    {
        return Err("not_regular_file".to_owned());
    }
    Ok(path)
}

// Deliberately limited credential guard, not a general secret detector. Never
// emit a signature from a source containing these recognized credential forms.
fn likely_credential(source: &str) -> bool {
    if source.contains("PRIVATE KEY-----") {
        return true;
    }
    for word in source.split(|c: char| !(c.is_ascii_alphanumeric() || c == '-' || c == '_')) {
        if (word.starts_with("github_pat_") && word.len() >= 51)
            || (["ghp_", "gho_", "ghu_", "ghs_", "ghr_"]
                .iter()
                .any(|prefix| word.starts_with(prefix))
                && word.len() >= 34)
            || (word.starts_with("sk-") && word.len() >= 35)
        {
            return true;
        }
    }
    for tail in source.split("://").skip(1) {
        let authority = tail
            .split(['/', ' ', '\n', '\r', '\t', '"', '\''])
            .next()
            .unwrap_or("");
        if authority
            .split('@')
            .next()
            .is_some_and(|user| user.contains(':'))
            && authority.contains('@')
        {
            return true;
        }
    }
    false
}

fn name_leaf(mut node: Node<'_>) -> Option<Node<'_>> {
    for _ in 0..24 {
        if matches!(
            node.kind(),
            "identifier"
                | "type_identifier"
                | "field_identifier"
                | "property_identifier"
                | "private_property_identifier"
                | "namespace_identifier"
                | "operator_name"
                | "destructor_name"
        ) {
            return Some(node);
        }
        let child = [
            "name",
            "declarator",
            "attribute",
            "property",
            "field",
            "function",
        ]
        .iter()
        .find_map(|field| node.child_by_field_name(field));
        match child {
            Some(child) => node = child,
            None => return None,
        }
    }
    None
}

fn declaration<'a>(language: Lang, node: Node<'a>) -> Option<(&'static str, Node<'a>)> {
    let kind = match (language, node.kind()) {
        (Lang::Python, "function_definition") => "function",
        (Lang::Python, "class_definition") => "class",
        (
            Lang::Javascript | Lang::Typescript | Lang::Tsx,
            "function_declaration" | "generator_function_declaration" | "function_signature",
        ) => "function",
        (
            Lang::Javascript | Lang::Typescript | Lang::Tsx,
            "method_definition" | "method_signature" | "abstract_method_signature",
        ) => "method",
        (
            Lang::Javascript | Lang::Typescript | Lang::Tsx,
            "class_declaration" | "abstract_class_declaration",
        ) => "class",
        (Lang::Typescript | Lang::Tsx, "interface_declaration") => "interface",
        (Lang::Typescript | Lang::Tsx, "type_alias_declaration") => "type",
        (Lang::Typescript | Lang::Tsx, "enum_declaration") => "enum",
        (Lang::Javascript | Lang::Typescript | Lang::Tsx, "variable_declarator") => {
            if !node.child_by_field_name("value").is_some_and(|value| {
                matches!(
                    value.kind(),
                    "arrow_function" | "function_expression" | "generator_function"
                )
            }) {
                return None;
            }
            "function"
        }
        (Lang::Rust, "function_item" | "function_signature_item") => "function",
        (Lang::Rust, "struct_item") => "struct",
        (Lang::Rust, "enum_item") => "enum",
        (Lang::Rust, "trait_item") => "trait",
        (Lang::Rust, "type_item") => "type",
        (Lang::Rust, "macro_definition") => "macro",
        (Lang::Rust, "const_item" | "static_item") => "constant",
        (Lang::Go, "function_declaration") => "function",
        (Lang::Go, "method_declaration" | "method_elem") => "method",
        (Lang::Go, "type_spec" | "type_alias") => "type",
        (Lang::C | Lang::Cpp, "function_definition") => "function",
        (Lang::C | Lang::Cpp, "function_declarator") => "declaration",
        (Lang::C | Lang::Cpp, "struct_specifier") => "struct",
        (Lang::C | Lang::Cpp, "enum_specifier") => "enum",
        (Lang::C | Lang::Cpp, "union_specifier") => "union",
        (Lang::Cpp, "class_specifier") => "class",
        (Lang::Cpp, "namespace_definition") => "namespace",
        (Lang::C | Lang::Cpp, "type_definition") | (Lang::Cpp, "alias_declaration") => "type",
        _ => return None,
    };
    let name = node
        .child_by_field_name("name")
        .or_else(|| node.child_by_field_name("declarator"))?;
    Some((kind, name_leaf(name)?))
}

fn reference_name(node: Node<'_>) -> Option<Node<'_>> {
    match node.kind() {
        "call" | "call_expression" => name_leaf(node.child_by_field_name("function")?),
        "new_expression" => name_leaf(node.child_by_field_name("constructor")?),
        "macro_invocation" => name_leaf(node.child_by_field_name("macro")?),
        "type_identifier" => Some(node),
        _ => None,
    }
}

fn parse(source: &str, language: Lang, deadline: Instant) -> Result<Parsed, String> {
    let mut parser = Parser::new();
    parser
        .set_language(&language.language())
        .map_err(|_| "incompatible_parser".to_owned())?;
    let mut expired = |_: &tree_sitter::ParseState| Instant::now() >= deadline;
    let tree = parser
        .parse_with_options(
            &mut |offset, _| source.as_bytes().get(offset..).unwrap_or_default(),
            None,
            Some(ParseOptions::new().progress_callback(&mut expired)),
        )
        .ok_or_else(|| "parse_timeout".to_owned())?;
    let mut result = Parsed {
        definitions: Vec::new(),
        references: Vec::new(),
        syntax_errors: tree.root_node().has_error(),
        partial: false,
        nodes: 0,
    };
    let mut declarations = HashSet::new();
    let mut references = HashSet::new();
    let mut cursor = tree.walk();
    'walk: loop {
        let node = cursor.node();
        result.nodes += 1;
        if result.nodes > MAX_NODES_PER_FILE
            || result.definitions.len() >= 4096
            || result.references.len() >= 16384
            || (result.nodes % 256 == 0 && Instant::now() >= deadline)
        {
            result.partial = true;
            break;
        }
        if node.is_named() && !node.is_error() && !node.is_missing() {
            if let Some((kind, name)) = declaration(language, node) {
                if let Some(text) = source
                    .get(name.byte_range())
                    .filter(|text| !text.is_empty() && text.len() <= 256)
                {
                    if declarations.insert(name.start_byte()) {
                        let header_end = node
                            .child_by_field_name("body")
                            .map_or(node.end_byte(), |body| body.start_byte());
                        let header = source.get(node.start_byte()..header_end).unwrap_or(text);
                        let signature = clip(header, 320)
                            .split_whitespace()
                            .collect::<Vec<_>>()
                            .join(" ");
                        let end = node.end_position();
                        result.definitions.push(Definition {
                            name: text.to_owned(),
                            kind,
                            line: node.start_position().row + 1,
                            end_line: (end.row + usize::from(end.column > 0))
                                .max(node.start_position().row + 1),
                            signature,
                            signature_truncated: header.chars().take(321).count() > 320,
                        });
                    }
                }
            }
            if let Some(name) = reference_name(node) {
                if !declarations.contains(&name.start_byte())
                    && references.insert(name.start_byte())
                {
                    if let Some(text) = source
                        .get(name.byte_range())
                        .filter(|text| !text.is_empty() && text.len() <= 256)
                    {
                        result.references.push(Reference {
                            name: text.to_owned(),
                            line: name.start_position().row + 1,
                        });
                    }
                }
            }
        }
        if cursor.goto_first_child() {
            continue;
        }
        loop {
            if cursor.goto_next_sibling() {
                break;
            }
            if !cursor.goto_parent() {
                break 'walk;
            }
        }
    }
    Ok(result)
}

fn words(value: &str) -> BTreeSet<String> {
    // Word splitting only personalizes the graph; symbol extraction is AST-based.
    let mut expanded = String::with_capacity(value.len() + 16);
    let mut previous_lower = false;
    for character in value.chars() {
        if character.is_uppercase() && previous_lower {
            expanded.push(' ');
        }
        expanded.push(character);
        previous_lower = character.is_lowercase();
    }
    let mut result: BTreeSet<String> = value
        .split(|character: char| !character.is_alphanumeric() && character != '_')
        .filter(|part| part.len() >= 2)
        .take(64)
        .map(str::to_lowercase)
        .collect();
    for part in expanded
        .split(|character: char| !character.is_alphanumeric())
        .filter(|part| part.len() >= 2)
        .take(64)
    {
        result.insert(part.to_lowercase());
    }
    result
}
fn relevance(value: &str, terms: &BTreeSet<String>) -> usize {
    words(value).intersection(terms).count()
}

struct Ranking {
    files: Vec<f64>,
    symbols: Vec<Vec<f64>>,
    incoming: Vec<usize>,
    edges: usize,
    matched: usize,
    partial: bool,
}
fn rank(
    files: &[File],
    terms: &BTreeSet<String>,
    focus: &BTreeSet<String>,
    deadline: Instant,
) -> Ranking {
    let count = files.len();
    if count == 0 {
        return Ranking {
            files: Vec::new(),
            symbols: Vec::new(),
            incoming: Vec::new(),
            edges: 0,
            matched: 0,
            partial: false,
        };
    }
    let mut definitions: BTreeMap<(&str, &str), Vec<(usize, usize)>> = BTreeMap::new();
    for (index, file) in files.iter().enumerate() {
        for (symbol, definition) in file.definitions.iter().enumerate() {
            definitions
                .entry((file.language.family(), &definition.name))
                .or_default()
                .push((index, symbol));
        }
    }
    let mut graph: Vec<BTreeMap<usize, f64>> = vec![BTreeMap::new(); count];
    let mut incoming = vec![0; count];
    let mut links = Vec::new();
    let mut partial = false;
    let mut matched = 0;
    'build: for (origin, file) in files.iter().enumerate() {
        let mut references = BTreeMap::<&str, usize>::new();
        for reference in &file.references {
            *references.entry(&reference.name).or_default() += 1;
        }
        for (name, repetitions) in references {
            let Some(targets) = definitions.get(&(file.language.family(), name)) else {
                continue;
            };
            // Do not turn a common name into an unbounded quadratic graph.
            if targets.len() > 64 {
                partial = true;
                continue;
            }
            matched += repetitions;
            let weight = (repetitions as f64).sqrt() / targets.len() as f64;
            for &(target, symbol) in targets {
                if links.len() >= MAX_EDGES || Instant::now() >= deadline {
                    partial = true;
                    break 'build;
                }
                *graph[origin].entry(target).or_default() += weight;
                incoming[target] += repetitions;
                links.push((origin, target, symbol, weight));
            }
        }
    }
    let mut personalization: Vec<f64> = files
        .iter()
        .map(|file| {
            1.0 + if focus.contains(&file.path) {
                20.0
            } else {
                0.0
            } + 4.0 * relevance(&file.path, terms) as f64
                + file
                    .definitions
                    .iter()
                    .map(|definition| 6.0 * relevance(&definition.name, terms) as f64)
                    .sum::<f64>()
                    .min(60.0)
        })
        .collect();
    let total: f64 = personalization.iter().sum();
    for value in &mut personalization {
        *value /= total;
    }
    let mut scores = personalization.clone();
    let outgoing: Vec<f64> = graph.iter().map(|edges| edges.values().sum()).collect();
    for _ in 0..40 {
        if Instant::now() >= deadline {
            partial = true;
            break;
        }
        let dangling: f64 = scores
            .iter()
            .zip(&outgoing)
            .filter(|(_, weight)| **weight == 0.0)
            .map(|(score, _)| score)
            .sum();
        let mut next: Vec<f64> = personalization
            .iter()
            .map(|value| (0.15 + 0.85 * dangling) * value)
            .collect();
        for (origin, edges) in graph.iter().enumerate() {
            if outgoing[origin] > 0.0 {
                for (&target, &weight) in edges {
                    next[target] += 0.85 * scores[origin] * weight / outgoing[origin];
                }
            }
        }
        let difference: f64 = next
            .iter()
            .zip(&scores)
            .map(|(left, right)| (left - right).abs())
            .sum();
        scores = next;
        if difference < 1e-9 {
            break;
        }
    }
    let mut symbols: Vec<Vec<f64>> = files
        .iter()
        .enumerate()
        .map(|(index, file)| {
            file.definitions
                .iter()
                .map(|definition| {
                    scores[index] / (file.definitions.len().max(1) as f64).sqrt()
                        + 2.0 * relevance(&definition.name, terms) as f64
                        + 0.1 * relevance(&file.path, terms) as f64
                        + if focus.contains(&file.path) {
                            0.25
                        } else {
                            0.0
                        }
                })
                .collect()
        })
        .collect();
    for (origin, target, symbol, weight) in &links {
        if outgoing[*origin] > 0.0 {
            symbols[*target][*symbol] += scores[*origin] * weight / outgoing[*origin];
        }
    }
    Ranking {
        files: scores,
        symbols,
        incoming,
        edges: links.len(),
        matched,
        partial,
    }
}

fn fit(mut value: Value, max_bytes: usize) -> Result<Value, String> {
    loop {
        if serde_json::to_vec(&value)
            .map_err(|_| "Cannot serialize repository map".to_owned())?
            .len()
            + 2
            <= max_bytes
        {
            return Ok(value);
        }
        value["truncated"] = json!(true);
        // Preserve the highest ranked definition and remove low-ranked detail.
        let symbols = value["symbols"].as_array().unwrap().len();
        let neighbors = value["neighbors"].as_array().unwrap().len();
        let category = if neighbors > 1 && neighbors * 2 > symbols {
            "neighbors"
        } else if symbols > 1 {
            "symbols"
        } else if neighbors > 0 {
            "neighbors"
        } else {
            ""
        };
        if !category.is_empty() {
            value[category].as_array_mut().unwrap().pop();
            let dropped = value["omitted"][category].as_u64().unwrap_or(0);
            value["omitted"][category] = json!(dropped + 1);
        } else if symbols == 1 && value.get("metadata_compact").is_none() {
            // A small byte budget should still carry useful code navigation.
            // Keep identity, counts and the parser for the retained definition;
            // verbose capability lists must not crowd out the last result.
            value["metadata_compact"] = json!(true);
            value["limitations"] = json!("Syntactic links; omitted files unverified. Hashes bind observed bytes, not atomic/current state.");
            let language = value["symbols"][0]["language"]
                .as_str()
                .unwrap_or("")
                .to_owned();
            let parser = if language == "typescript" || language == "tsx" {
                "typescript_tsx"
            } else {
                language.as_str()
            };
            let versions = value["scan"]["parser_versions"].as_object_mut().unwrap();
            let count = versions.len();
            versions.retain(|key, _| key == "tree_sitter" || key == parser);
            let omitted = count - versions.len();
            value["scan"]["parser_versions_omitted"] = json!(omitted);
            let scan = value["scan"].as_object_mut().unwrap();
            if let Some(languages) = scan.remove("supported_languages") {
                scan.insert(
                    "supported_languages_omitted".to_owned(),
                    json!(languages.as_array().unwrap().len()),
                );
            }
        } else if !value["scan"]["issues"].as_array().unwrap().is_empty() {
            value["scan"]["issues"].as_array_mut().unwrap().pop();
            let omitted = value["scan"]["omitted_issues"].as_u64().unwrap_or(0);
            value["scan"]["omitted_issues"] = json!(omitted + 1);
        } else if !value["scan"]["focus_not_observed"]
            .as_array()
            .unwrap()
            .is_empty()
        {
            value["scan"]["focus_not_observed"]
                .as_array_mut()
                .unwrap()
                .pop();
            let omitted = value["scan"]["omitted_focus_not_observed"]
                .as_u64()
                .unwrap_or(0);
            value["scan"]["omitted_focus_not_observed"] = json!(omitted + 1);
        } else if value["query"]
            .as_str()
            .is_some_and(|query| !query.is_empty())
        {
            let query = value["query"].as_str().unwrap();
            let keep = query.chars().count() / 2;
            let clipped = clip(query, keep);
            value["query"] = json!(clipped);
            value["query_truncated"] = json!(true);
        } else if symbols > 0 {
            value["symbols"].as_array_mut().unwrap().pop();
            let omitted = value["omitted"]["symbols"].as_u64().unwrap_or(0);
            value["omitted"]["symbols"] = json!(omitted + 1);
        } else {
            return Err("max_bytes is too small for repository map identity and counts".to_owned());
        }
    }
}

pub fn run(request: &Value) -> Result<Value, String> {
    if !request.is_object() {
        return Err("RepoMap request must be an object".to_owned());
    }
    let max_bytes = integer(request, "max_bytes", 8192, 2048, 65536)?;
    let max_files = integer(request, "max_files", 4096, 1, 50000)?;
    let max_file_bytes = integer(request, "max_file_bytes", 262144, 1, 1048576)?;
    let max_total_bytes = integer(request, "max_total_bytes", 67108864, 1, 134217728)?;
    let timeout = integer(request, "timeout_ms", 10000, 100, 30000)?;
    let deadline = Instant::now() + Duration::from_millis(timeout as u64);
    let root_value = request
        .get("root")
        .and_then(Value::as_str)
        .ok_or_else(|| "root must be an absolute directory".to_owned())?;
    if !Path::new(root_value).is_absolute() {
        return Err("root must be an absolute directory".to_owned());
    }
    let root = fs::canonicalize(root_value).map_err(|_| "Source root is unavailable".to_owned())?;
    if !root.is_dir() {
        return Err("Source root must be a directory".to_owned());
    }
    let root_text = root
        .to_str()
        .ok_or_else(|| "Source root is not Unicode".to_owned())?;
    let query = request.get("query").map_or(Ok(""), |value| {
        value
            .as_str()
            .ok_or_else(|| "query must be a string".to_owned())
    })?;
    if query.len() > 4096 || query.contains('\0') {
        return Err("query exceeds its text bound".to_owned());
    }
    let focus: BTreeSet<String> = strings(request, "focus_files", 64, 4096)?
        .into_iter()
        .collect();
    if focus.iter().any(|name| !allowed_path(name)) {
        return Err("focus_files must be permitted relative source paths".to_owned());
    }
    let excludes = strings(request, "excludes", 128, 1024)?;
    let terms = words(query);
    let before_head = git_head_until(&root, deadline)?;
    let mut paths = source_paths_until(&root, &excludes, deadline)?;
    paths.sort();
    paths.dedup();
    let listed_digest =
        hex(&serde_json::to_vec(&paths).map_err(|_| "Cannot bind source paths".to_owned())?);
    let listed = paths.len();
    let mut scan = Scan::default();
    let mut selected = Vec::new();
    let mut classified = 0;
    for path in paths {
        if classified % 256 == 0 && Instant::now() >= deadline {
            scan.reasons.insert("timeout".to_owned());
            break;
        }
        classified += 1;
        if !allowed_path(&path) {
            scan.issue(&path, "denied_path");
            continue;
        }
        let Some(language) = Lang::for_path(&path) else {
            let extension = clip(
                Path::new(&path)
                    .extension()
                    .and_then(|value| value.to_str())
                    .unwrap_or("[no extension]"),
                32,
            );
            if scan.unsupported.contains_key(&extension) || scan.unsupported.len() < 16 {
                *scan.unsupported.entry(extension).or_default() += 1;
            } else {
                scan.unsupported_other += 1;
            }
            *scan
                .counts
                .entry("unsupported_language".to_owned())
                .or_default() += 1;
            continue;
        };
        selected.push((
            !focus.contains(&path),
            std::cmp::Reverse(relevance(&path, &terms)),
            path,
            language,
        ));
    }
    selected
        .sort_by(|left, right| (&left.0, &left.1, &left.2).cmp(&(&right.0, &right.1, &right.2)));
    let eligible = selected.len();
    if eligible > max_files {
        scan.reasons.insert("max_files".to_owned());
        scan.counts
            .insert("max_files".to_owned(), eligible - max_files);
    }
    let mut files = Vec::new();
    let (mut definitions, mut references, mut inspected) = (0, 0, 0);
    for (_, _, path, language) in selected.into_iter().take(max_files) {
        if Instant::now() >= deadline
            || scan.nodes >= MAX_NODES
            || definitions >= MAX_DEFINITIONS
            || references >= MAX_REFERENCES
        {
            scan.reasons.insert(
                if Instant::now() >= deadline {
                    "timeout"
                } else {
                    "graph_resource_limit"
                }
                .to_owned(),
            );
            break;
        }
        inspected += 1;
        let source_path = match guarded_path(&root, &path) {
            Ok(path) => path,
            Err(reason) => {
                scan.issue(&path, &reason);
                continue;
            }
        };
        let limit = max_file_bytes.min(max_total_bytes.saturating_sub(scan.bytes));
        if limit == 0 {
            scan.reasons.insert("max_total_bytes".to_owned());
            break;
        }
        let data = match read_regular(&source_path, limit as u64) {
            Ok(data) => data,
            Err(_) => {
                scan.issue(&path, "unreadable_changed_or_byte_limit");
                continue;
            }
        };
        if guarded_path(&root, &path).is_err() {
            scan.issue(&path, "path_changed");
            continue;
        }
        scan.bytes += data.len();
        let source = match std::str::from_utf8(&data) {
            Ok(source) if !source.contains('\0') => source,
            _ => {
                scan.issue(&path, "not_utf8_text");
                continue;
            }
        };
        if likely_credential(source) {
            scan.issue(&path, "possible_credential");
            continue;
        }
        let parsed = match parse(
            source,
            language,
            deadline.min(Instant::now() + Duration::from_millis(500)),
        ) {
            Ok(parsed) => parsed,
            Err(reason) => {
                scan.issue(&path, &reason);
                continue;
            }
        };
        scan.nodes += parsed.nodes;
        if parsed.syntax_errors {
            scan.issue(&path, "syntax_errors");
        }
        if parsed.partial {
            scan.issue(&path, "extraction_limit");
        }
        let mut parsed = parsed;
        if parsed.definitions.len() > MAX_DEFINITIONS - definitions
            || parsed.references.len() > MAX_REFERENCES - references
            || scan.nodes > MAX_NODES
        {
            scan.issue(&path, "graph_resource_limit");
        }
        parsed.definitions.truncate(MAX_DEFINITIONS - definitions);
        parsed.references.truncate(MAX_REFERENCES - references);
        definitions += parsed.definitions.len();
        references += parsed.references.len();
        files.push(File {
            path,
            sha: hex(&data),
            language,
            definitions: parsed.definitions,
            references: parsed.references,
            syntax_errors: parsed.syntax_errors,
        });
    }
    if scan
        .counts
        .get("unsupported_language")
        .copied()
        .unwrap_or(0)
        > 0
    {
        scan.reasons.insert("unsupported_language".to_owned());
    }
    let after_head = if Instant::now() < deadline {
        Some(git_head_until(&root, deadline)?)
    } else {
        scan.reasons.insert("timeout".to_owned());
        None
    };
    if after_head
        .as_ref()
        .is_some_and(|observed| observed != &before_head)
    {
        scan.reasons.insert("git_head_changed".to_owned());
    }
    let ranking = rank(&files, &terms, &focus, deadline);
    if ranking.partial {
        scan.reasons.insert("graph_resource_limit".to_owned());
    }
    let observed: BTreeMap<&str, &str> = files
        .iter()
        .map(|file| (file.path.as_str(), file.sha.as_str()))
        .collect();
    let root_identity = hex(root_text.as_bytes());
    let binding = json!({"root_identity": root_identity, "git_head": before_head, "listed_paths_sha256": listed_digest,
        "files": observed, "excludes": excludes});
    let content_digest =
        hex(&serde_json::to_vec(&binding).map_err(|_| "Cannot bind repository map".to_owned())?);
    let mut symbols = Vec::new();
    for (index, file) in files.iter().enumerate() {
        for position in 0..file.definitions.len() {
            symbols.push((ranking.symbols[index][position], index, position));
        }
    }
    symbols.sort_by(|left, right| {
        right
            .0
            .total_cmp(&left.0)
            .then(files[left.1].path.cmp(&files[right.1].path))
            .then(
                files[left.1].definitions[left.2]
                    .line
                    .cmp(&files[right.1].definitions[right.2].line),
            )
    });
    let symbol_count = symbols.len();
    let mut reference_examples: BTreeMap<(&str, &str), Vec<Value>> = BTreeMap::new();
    for file in &files {
        for reference in &file.references {
            let samples = reference_examples
                .entry((file.language.family(), &reference.name))
                .or_default();
            if samples.len() < 3 {
                samples.push(json!({"path": file.path, "line": reference.line}));
            }
        }
    }
    let symbols: Vec<Value> = symbols.into_iter().take(80).map(|(score, index, position)| {
        let file = &files[index]; let definition = &file.definitions[position];
        let mut reasons = vec!["graph_rank"];
        if relevance(&definition.name, &terms) > 0 { reasons.push("query_symbol"); }
        if relevance(&file.path, &terms) > 0 { reasons.push("query_path"); }
        if focus.contains(&file.path) { reasons.push("focus_file"); }
        json!({"name": definition.name, "kind": definition.kind, "path": file.path, "language": file.language.name(),
            "start_line": definition.line, "end_line": definition.end_line, "signature": definition.signature,
            "signature_truncated": definition.signature_truncated,
            "file_sha256": file.sha, "score": (score * SCORE_SCALE).round() as u64, "reasons": reasons,
            "source_parse_errors": file.syntax_errors,
            "syntactic_reference_examples": reference_examples.get(&(file.language.family(), definition.name.as_str())).cloned().unwrap_or_default()})
    }).collect();
    let mut neighbors: Vec<usize> = (0..files.len()).collect();
    neighbors.sort_by(|&left, &right| {
        ranking.files[right]
            .total_cmp(&ranking.files[left])
            .then(files[left].path.cmp(&files[right].path))
    });
    let neighbor_count = neighbors.len();
    let neighbors: Vec<Value> = neighbors.into_iter().take(20).map(|index| {
        let file = &files[index];
        json!({"path": file.path, "file_sha256": file.sha, "score": (ranking.files[index] * SCORE_SCALE).round() as u64,
            "definitions": file.definitions.len(), "incoming_name_matches": ranking.incoming[index]})
    }).collect();
    let focus_missing: Vec<_> = focus
        .iter()
        .filter(|path| !observed.contains_key(path.as_str()))
        .cloned()
        .collect();
    let value = json!({"schema_version": 1, "purpose": "syntactic_repo_map", "root_identity": root_identity,
        "query": clip(query, 512), "query_sha256": hex(query.as_bytes()), "query_truncated": query.chars().count() > 512,
        "revision": {"git_head": before_head, "git_head_unchanged": after_head.as_ref().map(|value| value == &before_head),
            "content_digest": content_digest, "listed_paths_sha256": listed_digest, "binding_scope": "observed_files",
            "complete": scan.reasons.is_empty(), "source_current": Value::Null},
        "scan": {"listed_files": listed, "unclassified_files": listed - classified, "eligible_files": eligible, "inspected_files": inspected, "parsed_files": files.len(),
            "source_bytes": scan.bytes, "definitions": definitions, "syntactic_references": references,
            "matched_syntactic_references": ranking.matched, "graph_edges": ranking.edges,
            "partial": !scan.reasons.is_empty(), "reasons": scan.reasons, "skipped": scan.counts, "issues": scan.issues,
            "omitted_issues": scan.omitted_issues, "unsupported_extensions": scan.unsupported, "unsupported_other": scan.unsupported_other,
            "focus_not_observed": focus_missing.iter().take(8).map(|value| clip(value, 240)).collect::<Vec<_>>(),
            "omitted_focus_not_observed": focus_missing.len().saturating_sub(8),
            "supported_languages": ["python", "javascript", "typescript", "tsx", "rust", "go", "c", "cpp"],
            "parser_versions": {"tree_sitter": "0.25.10", "python": "0.25.0", "javascript": "0.25.0", "typescript_tsx": "0.23.2",
                "rust": "0.24.0", "go": "0.25.0", "c": "0.24.1", "cpp": "0.23.4"}},
        "ranking": {"algorithm": "personalized_pagerank", "damping_percent": 85, "score_scale": 1000000},
        "symbols": symbols, "neighbors": neighbors,
        "omitted": {"symbols": symbol_count.saturating_sub(80), "neighbors": neighbor_count.saturating_sub(20)},
        "truncated": symbol_count > 80 || neighbor_count > 20, "max_output_bytes": max_bytes,
        "limitations": "Syntactic named definitions and call/type references; name links are not semantic resolution. Observed bytes, not an atomic or current repository snapshot. Unsupported/omitted files remain unverified."});
    fit(value, max_bytes)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicU64, Ordering};

    struct Fixture(PathBuf);
    impl Fixture {
        fn new() -> Self {
            static NEXT: AtomicU64 = AtomicU64::new(0);
            let base = std::env::temp_dir().join(format!(
                "harness-repomap-{}-{}-{}",
                std::process::id(),
                std::time::SystemTime::now()
                    .duration_since(std::time::UNIX_EPOCH)
                    .unwrap()
                    .as_nanos(),
                NEXT.fetch_add(1, Ordering::Relaxed)
            ));
            fs::create_dir(&base).unwrap();
            Self(fs::canonicalize(base).unwrap())
        }
        fn write(&self, name: &str, bytes: &[u8]) {
            let path = self.0.join(name);
            fs::create_dir_all(path.parent().unwrap()).unwrap();
            fs::write(path, bytes).unwrap();
        }
        fn request(&self) -> Value {
            json!({"root": self.0, "operation": "repomap", "timeout_ms": 30000})
        }
        fn git(&self, args: &[&str]) {
            let output = std::process::Command::new("git")
                .arg("-c")
                .arg(format!(
                    "core.hooksPath={}",
                    self.0.join("no-hooks").display()
                ))
                .args([
                    "-c",
                    "init.templateDir=",
                    "-c",
                    "commit.gpgsign=false",
                    "-c",
                    "user.name=Fixture",
                    "-c",
                    "user.email=fixture@example.invalid",
                    "-C",
                ])
                .arg(&self.0)
                .args(args)
                .output()
                .unwrap();
            assert!(
                output.status.success(),
                "Git fixture failed: {}",
                String::from_utf8_lossy(&output.stderr)
            );
        }
    }
    impl Drop for Fixture {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.0);
        }
    }

    fn has_symbol(value: &Value, name: &str) -> bool {
        value["symbols"]
            .as_array()
            .unwrap()
            .iter()
            .any(|item| item["name"] == name)
    }
    #[test]
    fn parses_eight_languages_with_real_ast_definitions_and_calls() {
        let cases = [
            (Lang::Python, "def parse_value(x):\n    return x\ndef caller():\n    return parse_value(1)\n", "parse_value"),
            (Lang::Javascript, "export function parseValue(x) { return x; } const caller = () => parseValue(1);", "parseValue"),
            (Lang::Typescript, "interface Value { id: number } function parseValue(x: Value): Value { return x; } const caller = () => parseValue({id:1});", "parseValue"),
            (Lang::Tsx, "function View() { return <div/>; } function caller() { return View(); }", "View"),
            (Lang::Rust, "pub fn parse_value(x: u8) -> u8 { x } fn caller() { parse_value(1); }", "parse_value"),
            (Lang::Go, "package demo\nfunc ParseValue(x int) int { return x }\nfunc caller() { ParseValue(1) }", "ParseValue"),
            (Lang::C, "int parse_value(int x) { return x; } int caller(void) { return parse_value(1); }", "parse_value"),
            (Lang::Cpp, "class Parser {}; int parse_value(int x) { return x; } int caller() { return parse_value(1); }", "parse_value"),
        ];
        for (language, source, name) in cases {
            let result = parse(source, language, Instant::now() + Duration::from_secs(5)).unwrap();
            assert!(!result.syntax_errors, "{} syntax", language.name());
            assert!(
                result.definitions.iter().any(|item| item.name == name),
                "{} missing definition",
                language.name()
            );
            assert!(
                result
                    .references
                    .iter()
                    .any(|item| item.name == name && item.line > 0),
                "{} missing reference",
                language.name()
            );
        }
    }
    #[test]
    fn comments_and_strings_do_not_create_definitions_or_references() {
        let source = "# def invented(): fake_call()\ntext = 'def invented(): fake_call()'\ndef real():\n    return 1\n";
        let result = parse(
            source,
            Lang::Python,
            Instant::now() + Duration::from_secs(1),
        )
        .unwrap();
        assert_eq!(result.definitions.len(), 1);
        assert_eq!(result.definitions[0].name, "real");
        assert!(result.references.is_empty());
    }
    #[test]
    fn parser_and_walk_deadline_are_bounded() {
        let source = "def value():\n    return value()\n".repeat(10000);
        let result = parse(&source, Lang::Python, Instant::now());
        assert!(result.is_err() || result.unwrap().partial);
    }
    #[test]
    fn credential_and_path_guards_are_explicit() {
        for name in [
            "../a.py",
            "/a.py",
            "a\\b.py",
            ".aws/a.py",
            ".env",
            "key.pem",
            "a/./b.py",
        ] {
            assert!(!allowed_path(name), "{name}");
        }
        assert!(allowed_path("src/данные parser.py"));
        assert!(likely_credential(&format!(
            "key = '{}'",
            "ghp_".to_owned() + &"x".repeat(40)
        )));
        let fixture = ["url='https://", "name:secret", "@example.test/path'"].concat();
        assert!(likely_credential(&fixture));
        assert!(!likely_credential("def parse_value(x): return x"));
    }
    #[test]
    fn personalization_promotes_task_symbol_and_retains_graph_neighbor() {
        let mut files = Vec::new();
        for (path, source) in [
            ("a_noise.py", "def noise(): return 1\n"),
            ("parser.py", "def parse_record(x): return x\n"),
            (
                "validation.py",
                "def validate_input(x): return parse_record(x)\n",
            ),
            ("main.py", "def main(x): return validate_input(x)\n"),
        ] {
            let parsed = parse(
                source,
                Lang::Python,
                Instant::now() + Duration::from_secs(1),
            )
            .unwrap();
            files.push(File {
                path: path.to_owned(),
                sha: hex(source.as_bytes()),
                language: Lang::Python,
                definitions: parsed.definitions,
                references: parsed.references,
                syntax_errors: false,
            });
        }
        let rank = rank(
            &files,
            &words("fix validate_input"),
            &BTreeSet::new(),
            Instant::now() + Duration::from_secs(1),
        );
        assert!(rank.symbols[2][0] > rank.symbols[0][0]);
        assert!(rank.symbols[2][0] > rank.symbols[1][0]);
        assert!(rank.files[1] > rank.files[0]);
        assert_eq!(rank.edges, 2);
        assert!(!rank.partial);
        let centrality = super::rank(
            &files,
            &BTreeSet::new(),
            &BTreeSet::new(),
            Instant::now() + Duration::from_secs(1),
        );
        assert!(
            centrality.files[1] > centrality.files[2],
            "Empty query uses graph centrality"
        );
    }
    #[test]
    fn request_ranks_real_source_and_returns_addressable_references_without_writes() {
        let fixture = Fixture::new();
        fixture.write("noise.py", b"def unrelated(): return 1\n");
        fixture.write("parser.py", b"def parse_record(x): return x\n");
        fixture.write(
            "validation.py",
            b"def validate_input(x): return parse_record(x)\n",
        );
        fixture.write("main.py", b"def main(x): return validate_input(x)\n");
        let before: BTreeMap<_, _> = fs::read_dir(&fixture.0)
            .unwrap()
            .map(|entry| {
                let path = entry.unwrap().path();
                (
                    path.file_name().unwrap().to_owned(),
                    fs::read(path).unwrap(),
                )
            })
            .collect();
        let mut request = fixture.request();
        request["query"] = json!("fix validate_input");
        let value = run(&request).unwrap();
        assert_eq!(value["symbols"][0]["name"], "validate_input");
        assert!(value["symbols"][0]["syntactic_reference_examples"]
            .as_array()
            .unwrap()
            .iter()
            .any(|item| item["path"] == "main.py" && item["line"] == 1));
        assert!(value["neighbors"]
            .as_array()
            .unwrap()
            .iter()
            .any(|item| item["path"] == "parser.py"));
        assert_eq!(value["scan"]["graph_edges"], 2);
        assert_eq!(value["scan"]["parsed_files"], 4);
        assert_eq!(value["scan"]["partial"], false);
        assert!(value["revision"]["source_current"].is_null());
        assert_eq!(
            value["symbols"][0]["file_sha256"],
            hex(b"def validate_input(x): return parse_record(x)\n")
        );
        let after: BTreeMap<_, _> = fs::read_dir(&fixture.0)
            .unwrap()
            .map(|entry| {
                let path = entry.unwrap().path();
                (
                    path.file_name().unwrap().to_owned(),
                    fs::read(path).unwrap(),
                )
            })
            .collect();
        assert_eq!(before, after);
    }
    #[test]
    fn late_focus_file_and_query_path_are_prioritized_before_file_limit() {
        let fixture = Fixture::new();
        for index in 0..10 {
            fixture.write(&format!("a{index}.py"), b"def noise(): return 1\n");
        }
        fixture.write("z_target.py", b"def critical_target(): return 1\n");
        let mut request = fixture.request();
        request["max_files"] = json!(1);
        request["focus_files"] = json!(["z_target.py"]);
        let value = run(&request).unwrap();
        assert!(has_symbol(&value, "critical_target"));
        assert_eq!(value["scan"]["parsed_files"], 1);
        assert_eq!(value["scan"]["partial"], true);
        assert_eq!(value["scan"]["skipped"]["max_files"], 10);
        request.as_object_mut().unwrap().remove("focus_files");
        request["query"] = json!("z_target");
        assert!(has_symbol(&run(&request).unwrap(), "critical_target"));
    }
    #[test]
    fn whole_source_hash_changes_for_uncommitted_edits_and_preserves_git_head() {
        let fixture = Fixture::new();
        fixture.write("parser.py", b"def parse_value(x): return x\n");
        fixture.git(&["init", "-q"]);
        fixture.git(&["add", "--", "parser.py"]);
        fixture.git(&["commit", "-qm", "Synthetic RepoMap fixture"]);
        let before = run(&fixture.request()).unwrap();
        fixture.write("parser.py", b"def parse_value(x): return x + 1\n");
        let after = run(&fixture.request()).unwrap();
        assert!(before["revision"]["git_head"].as_str().is_some());
        assert_eq!(
            before["revision"]["git_head"],
            after["revision"]["git_head"]
        );
        assert_ne!(
            before["revision"]["content_digest"],
            after["revision"]["content_digest"]
        );
        assert_ne!(
            before["symbols"][0]["file_sha256"],
            after["symbols"][0]["file_sha256"]
        );
    }
    #[test]
    fn non_git_gitignore_excludes_and_credential_guards_apply_to_inventory() {
        let fixture = Fixture::new();
        fixture.write(".gitignore", b"ignored.py\n");
        fixture.write("ignored.py", b"def MUST_NOT_APPEAR(): return 1\n");
        fixture.write("excluded.py", b"def ALSO_ABSENT(): return 1\n");
        fixture.write("ok.py", b"def visible(): return 1\n");
        fixture.write(".aws/config.py", b"def PRIVATE_NAME(): return 1\n");
        fixture.write(
            "inline.py",
            format!(
                "credential = '{}'\ndef HIDDEN(): return 1\n",
                "ghp_".to_owned() + &"x".repeat(40)
            )
            .as_bytes(),
        );
        let mut request = fixture.request();
        request["excludes"] = json!(["excluded.py"]);
        let value = run(&request).unwrap();
        let wire = serde_json::to_string(&value).unwrap();
        assert!(has_symbol(&value, "visible"));
        for absent in [
            "MUST_NOT_APPEAR",
            "ALSO_ABSENT",
            "PRIVATE_NAME",
            "HIDDEN",
            "ghp_",
        ] {
            assert!(!wire.contains(absent), "{absent}");
        }
        assert_eq!(value["scan"]["skipped"]["possible_credential"], 1);
        assert_eq!(value["scan"]["partial"], true);
    }
    #[test]
    fn unsupported_parse_errors_invalid_utf8_and_byte_limits_are_not_complete() {
        let fixture = Fixture::new();
        fixture.write("ok.py", b"def good(): return 1\n");
        fixture.write("unknown.java", b"class Example {}\n");
        fixture.write("broken.py", b"def (\n");
        fixture.write("binary.py", b"\xff\x00");
        fixture.write("large.py", &vec![b'x'; 2000]);
        let mut request = fixture.request();
        request["max_file_bytes"] = json!(1000);
        let value = run(&request).unwrap();
        assert!(has_symbol(&value, "good"));
        assert_eq!(value["scan"]["partial"], true);
        assert_eq!(value["revision"]["complete"], false);
        for reason in [
            "unsupported_language",
            "syntax_errors",
            "not_utf8_text",
            "unreadable_changed_or_byte_limit",
        ] {
            assert_eq!(value["scan"]["skipped"][reason], 1, "{reason}");
        }
    }
    #[test]
    fn utf8_serialized_byte_budget_and_counts_survive_output_truncation() {
        let fixture = Fixture::new();
        let source: String = (0..70)
            .map(|index| format!("def parse_{index}(x):\n    return x\n"))
            .collect();
        fixture.write("данные parser.py", source.as_bytes());
        for budget in [2048, 4096, 8192] {
            let mut request = fixture.request();
            request["max_bytes"] = json!(budget);
            request["query"] = json!("\u{1}🙂\\\"".repeat(100));
            let value = run(&request).unwrap();
            assert!(serde_json::to_vec(&value).unwrap().len() + 2 <= budget as usize);
            assert_eq!(value["scan"]["definitions"], 70);
            assert_eq!(
                value["symbols"].as_array().unwrap().len() as u64
                    + value["omitted"]["symbols"].as_u64().unwrap(),
                70
            );
            assert_eq!(value["truncated"], true);
        }
    }
    #[test]
    fn rejects_invalid_request_limits_before_reading_source() {
        for value in [json!(true), json!(1.5), json!(-1), json!(0), json!(50001)] {
            assert!(run(&json!({"root": "/does/not/exist", "max_files": value}))
                .unwrap_err()
                .contains("max_files"));
        }
        let fixture = Fixture::new();
        let mut request = fixture.request();
        request["focus_files"] = json!(["../outside.py"]);
        assert!(run(&request).is_err());
    }
    #[cfg(unix)]
    #[test]
    fn symlink_file_and_parent_do_not_expose_outside_symbols() {
        use std::os::unix::fs::symlink;
        let fixture = Fixture::new();
        let outside = Fixture::new();
        outside.write("outside.py", b"def OUTSIDE_SENTINEL(): return 1\n");
        fixture.write("ok.py", b"def visible(): return 1\n");
        symlink(outside.0.join("outside.py"), fixture.0.join("link.py")).unwrap();
        symlink(&outside.0, fixture.0.join("linked_directory")).unwrap();
        assert!(guarded_path(&fixture.0, "linked_directory/outside.py").is_err());
        let value = run(&fixture.request()).unwrap();
        assert!(has_symbol(&value, "visible"));
        assert!(!serde_json::to_string(&value)
            .unwrap()
            .contains("OUTSIDE_SENTINEL"));
    }
}
