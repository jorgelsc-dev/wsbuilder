# Changelog

All notable changes to `wsbuilder` are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[semantic versioning](https://semver.org/). Versions are released automatically
from `main` (see `CONTRIBUTING.md`): when a release is cut, the
**Unreleased** section becomes the opening of its GitHub Release notes, and the
next pull request moves those entries under the new version heading.

## [Unreleased]

### Security

- HTTP/2: streams reset by the client keep their `MAX_CONCURRENT_STREAMS` slot
  until their handler finishes, queued handlers of reset streams no longer run,
  and a connection that resets too many streams is closed with
  `ENHANCE_YOUR_CALM` ("Rapid Reset", CVE-2023-44487). PING/SETTINGS floods,
  empty-frame floods and CONTINUATION floods are bounded the same way.
- WebSocket handshakes check `Origin`: by default only same-origin pages and
  clients without `Origin` are accepted (Cross-Site WebSocket Hijacking). Use
  `App(ws_allowed_origins=...)`, `@app.ws(..., allowed_origins=...)` or
  `check_origin=` to allow other origins (#87).
- Metrics endpoints (`/api/metrics`, `/api/metrics/stream`) and the ProxyI
  dashboard answer only loopback and private networks by default, accept
  `access=`, `token=` and `authorize=`, and bound NDJSON streams in number and
  duration (#86).
- HTTP/1.1 rejects bare-LF line endings at once and treats methods as
  case-sensitive; header values reject `DEL` (#89, #91).

### Added

- Path parameters in routes: `/users/{id:int}`, `/files/{rest:path}`, exposed as
  `request.path_params`; static routes take precedence (#88).
- `App(redirect_slashes=True)`: `/items/` answers 308 to `/items` when only the
  other spelling exists (#88).
- `HTTPServer` options `max_ws_connections` and `max_http2_handler_threads`;
  WebSocket connections no longer hold HTTP worker slots (#90).
- Absolute-form request targets and `OPTIONS *` (#89).
- `.github/SECURITY.md` with the private reporting process (#85).
- `benchmarks/http1_keepalive.py` (#91).
- `wsbuilder.weights`: standard-library readers for safetensors (single file
  and sharded `*.index.json`) and GGUF v2/v3 weight files over `mmap`, with
  zero-copy `raw()` views and float conversion of F32/F16/BF16/F64/Q8_0/Q4_0
  tensors. `ModelStore` imports, summarizes and opens these formats;
  `load()` still runs only `wsbuilder.bytelm` models.
- `wsbuilder.bpe`: standard-library byte-level BPE tokenizer that loads
  Hugging Face `tokenizer.json` or GGUF tokenizer metadata, with Qwen2/Qwen3,
  Llama 3 and GPT-2 pre-tokenizers, streaming decode and the Qwen3 chat
  template (`format_qwen3_chat`). `encode_chat` builds the chat prompt from
  the template's control tokens and encodes message text without matching
  any added token, so user text cannot inject turns or tool-call markers.

### Changed

- The package version is derived from git tags with `setuptools-scm`; every
  commit gets a distinct version, so `pip install -U git+...` always updates
  (#69, #72). Releases tag the tested `main` commit directly: no
  `release/vX.Y.Z` branches and no version-bump pull requests (#70, #73).
- Releases are cut only when shipped files change (`src/`, `pyproject.toml`,
  `README.md`, ...), not for docs, CI or tests alone (#71).
- `publish-packages.yml` runs only when dispatched on a tag (#75).
- Connections over capacity get `503 Service Unavailable` with `Retry-After`
  instead of a reset (#90).
- `OPTIONS` answers `204` with an `Allow` header; `Access-Control-*` headers are
  sent only when CORS is enabled (#89).
- `wsbuilder.proxyi` is now a package (`_common`, `stats`, `routing`, `core`);
  every name is still importable from `wsbuilder.proxyi` (#78).
- The IA and LLM modules are documented as experimental (#79).

### Fixed

- `Database(...)` closes its SQLite connection when a pragma or the replica
  setup fails (#84).
- A refused HTTP/2 stream no longer turns the client's later `RST_STREAM` into a
  connection error.
- Silent `except Exception: pass` blocks now catch specific errors or log them;
  ruff rule `S110` keeps new ones out (#77).
- README links work on PyPI; every documentation page is in the site menu (#82,
  #83).

## [1.5.0] - 2026-11-16

### Changed

- NDJSON is the only format for language models and network weights; native
  pure-Python language model engine (`ByteLanguageModel`).

Earlier releases are described in their
[GitHub Releases](https://github.com/jorgelsc-dev/wsbuilder/releases).
