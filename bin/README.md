# Native helpers

Release archives include `linux-x86_64/` and `windows-x86_64/` bundles here.
Each bundle contains `harness-native` (or `.exe`), `build-info.json`, and third-party
license notices. Generated executables and build directories are not Git source.

Build the current platform from the repository with:

```text
python tools/build_native.py
python tools/harness.py native status
```

Source builds require Rust 1.90.0 and a C toolchain for the Tree-sitter grammars.
The prebuilt helper does not need Cargo, Rust or a C compiler at runtime.
Ordinary harness commands never download dependencies or compile on demand.
