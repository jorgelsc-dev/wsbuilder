"""Language models: import, store, download and run them.

Every model in wsbuilder is one NDJSON file (see :mod:`wsbuilder.ndjson_model`):
the first line describes the model and each following line holds one tensor
of weights. Three independent pieces work with those files:

* :class:`ModelStore` keeps model files in one directory, each with a JSON
  manifest (name, format, size, SHA-256, source and a metadata summary read
  from the file). Files can be imported from disk, downloaded, exported and
  removed. Only wsbuilder NDJSON model files are accepted.
* :func:`download_file` and :func:`huggingface_files` fetch models from any
  HTTPS URL or from the Hugging Face Hub: resumable, size-limited, verified
  against the SHA-256 the Hub publishes, and never leaking a token across a
  redirect to another host.
* :class:`LanguageModel` runs a model through a backend. Stored models run
  natively in this process with :class:`~wsbuilder.ia_lm.NativeBackend`
  (wsbuilder's own pure-Python language model, architecture
  ``wsbuilder.bytelm``); any server speaking the OpenAI API can also be used
  through :class:`OpenAICompatibleBackend`.

:func:`install_llm` exposes a store and its models over HTTP, including an
OpenAI-compatible ``/v1/chat/completions`` endpoint.

Everything here needs nothing outside the standard library.
"""

import hashlib
import json
import os
import re
import shutil
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from .ndjson_model import ModelFormatError, ModelReader, is_model_file

__all__ = [
    "DEFAULT_HF_ENDPOINT",
    "DEFAULT_TRAIN_LIMITS",
    "DownloadError",
    "LanguageModel",
    "ModelRecord",
    "ModelStore",
    "OpenAICompatibleBackend",
    "default_model_root",
    "detect_model_format",
    "download_file",
    "huggingface_files",
    "huggingface_url",
    "install_llm",
    "summarize_model_file",
]

DEFAULT_HF_ENDPOINT = "https://huggingface.co"
#: Downloads larger than this are refused unless the caller raises the limit.
DEFAULT_MAX_DOWNLOAD_BYTES = 200 * 1024**3
_CHUNK = 1024 * 1024
_MANIFEST = "manifest.json"
_SAFE_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class DownloadError(OSError):
    """A download that failed, was refused, or did not verify."""


def default_model_root():
    """``$WSBUILDER_MODELS``, else ``~/.cache/wsbuilder/models``."""
    configured = os.environ.get("WSBUILDER_MODELS")
    if configured:
        return os.path.abspath(os.path.expanduser(configured))
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    return os.path.join(base, "wsbuilder", "models")


# -- file formats -----------------------------------------------------------


def detect_model_format(path):
    """``"ndjson"`` for a wsbuilder model file, else ``"unknown"``."""
    return "ndjson" if is_model_file(path) else "unknown"


def summarize_model_file(path):
    """A metadata summary of a model file (just its size for other files)."""
    if detect_model_format(path) == "ndjson":
        with ModelReader(path) as reader:
            return reader.summary()
    return {"format": "unknown", "file_size": os.path.getsize(path)}


def _sha256_of(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _slug(text):
    slug = re.sub(r"[^a-z0-9._-]+", "-", str(text).lower()).strip("-._")
    return (slug or "model")[:100]


def _safe_filename(name):
    base = os.path.basename(str(name).replace("\\", "/"))
    if base in ("", ".", "..") or base.startswith("."):
        raise ValueError(f"unsafe file name {name!r}")
    return base


# -- downloads --------------------------------------------------------------


class _SameHostAuthRedirect(urllib.request.HTTPRedirectHandler):
    """Follow redirects, but drop credentials when the host changes.

    The Hub redirects model files to a CDN; sending the user's token there
    would hand it to a third party.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is None:
            return None
        old_host = urllib.parse.urlsplit(req.full_url).netloc.lower()
        new_parts = urllib.parse.urlsplit(newurl)
        if new_parts.netloc.lower() != old_host:
            for header in ("Authorization", "Cookie"):
                new.headers.pop(header, None)
                new.unredirected_hdrs.pop(header, None)
        if urllib.parse.urlsplit(req.full_url).scheme == "https" and new_parts.scheme != "https":
            raise urllib.error.HTTPError(newurl, code, "refusing to redirect from HTTPS to HTTP", headers, fp)
        return new


def _opener():
    return urllib.request.build_opener(_SameHostAuthRedirect())


def _check_url(url, allow_http):
    parts = urllib.parse.urlsplit(str(url))
    if parts.scheme == "https":
        return
    if parts.scheme == "http" and allow_http:
        return
    raise DownloadError(f"only https:// downloads are allowed, not {url!r}")


def download_file(
    url,
    destination,
    *,
    sha256=None,
    headers=None,
    token=None,
    resume=True,
    progress=None,
    timeout=60.0,
    max_bytes=DEFAULT_MAX_DOWNLOAD_BYTES,
    allow_http=False,
    cancel_event=None,
):
    """Download ``url`` to ``destination``, atomically, and return its SHA-256.

    Bytes go to ``destination + ".part"``; an interrupted download resumes
    from there with a Range request when the server supports it. When
    ``sha256`` is given the result must match it, or the file is deleted.
    ``progress(done, total)`` is called as bytes arrive (``total`` may be
    None). ``token`` is sent as a bearer token to the first host only.
    """
    _check_url(url, allow_http)
    expected = str(sha256).lower() if sha256 else None
    if expected is not None and not _SHA256.match(expected):
        raise ValueError("sha256 must be 64 hexadecimal characters")
    destination = os.fspath(destination)
    partial = destination + ".part"
    os.makedirs(os.path.dirname(os.path.abspath(destination)) or ".", exist_ok=True)
    request_headers = {"User-Agent": "wsbuilder-llm"}
    request_headers.update(headers or {})
    if token:
        request_headers["Authorization"] = f"Bearer {token}"

    offset = os.path.getsize(partial) if resume and os.path.exists(partial) else 0
    if offset and expected is None:
        # Without a checksum a resumed file cannot be trusted to be one file.
        offset = 0
    if offset:
        request_headers["Range"] = f"bytes={offset}-"
    request = urllib.request.Request(url, headers=request_headers)
    try:
        response = _opener().open(request, timeout=timeout)
    except urllib.error.HTTPError as exc:
        if exc.code == 416 and offset:
            # The partial file is already complete (or bigger): start over.
            os.unlink(partial)
            return download_file(
                url, destination, sha256=sha256, headers=headers, token=token, resume=False,
                progress=progress, timeout=timeout, max_bytes=max_bytes, allow_http=allow_http,
                cancel_event=cancel_event,
            )
        raise DownloadError(f"download failed: HTTP {exc.code} for {url}") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise DownloadError(f"download failed: {getattr(exc, 'reason', exc)}") from exc

    digest = hashlib.sha256()
    with response:
        status = getattr(response, "status", 200)
        if offset and status == 206:
            mode = "ab"
            with open(partial, "rb") as handle:
                for chunk in iter(lambda: handle.read(_CHUNK), b""):
                    digest.update(chunk)
        else:
            mode, offset = "wb", 0
        length = response.headers.get("Content-Length")
        total = offset + int(length) if length and length.isdigit() else None
        if total is not None and total > max_bytes:
            raise DownloadError(f"download is {total} bytes, over max_bytes={max_bytes}")
        done = offset
        with open(partial, mode) as handle:
            while True:
                if cancel_event is not None and cancel_event.is_set():
                    raise DownloadError("download cancelled")
                try:
                    chunk = response.read(_CHUNK)
                except OSError as exc:
                    raise DownloadError(f"download interrupted after {done} bytes: {exc}") from exc
                if not chunk:
                    break
                done += len(chunk)
                if done > max_bytes:
                    raise DownloadError(f"download exceeds max_bytes={max_bytes}")
                handle.write(chunk)
                digest.update(chunk)
                if progress is not None:
                    progress(done, total)
            handle.flush()
            os.fsync(handle.fileno())
    if total is not None and done != total:
        raise DownloadError(f"download ended at {done} of {total} bytes")
    actual = digest.hexdigest()
    if expected is not None and actual != expected:
        os.unlink(partial)
        raise DownloadError(f"SHA-256 mismatch: expected {expected}, got {actual}")
    os.replace(partial, destination)
    return actual


def huggingface_url(repo, filename, *, revision="main", endpoint=None):
    """The download URL of ``filename`` in a Hub model repository."""
    endpoint = (endpoint or os.environ.get("HF_ENDPOINT") or DEFAULT_HF_ENDPOINT).rstrip("/")
    repo = str(repo).strip("/")
    if repo.count("/") != 1 or ".." in repo:
        raise ValueError("repo must look like 'owner/name'")
    quoted = urllib.parse.quote(str(filename).lstrip("/"))
    return f"{endpoint}/{repo}/resolve/{urllib.parse.quote(str(revision), safe='')}/{quoted}"


def huggingface_files(repo, *, revision="main", endpoint=None, token=None, timeout=30.0, suffix=None,
                      allow_http=False):
    """Files in a Hub model repository: ``[{"path", "size", "sha256"}, ...]``.

    ``sha256`` is the LFS object id the Hub publishes for large files (every
    model weight file); pass it to :func:`download_file` to verify the bytes.
    ``suffix`` filters by extension, e.g. ``".ndjson"``.
    """
    endpoint = (endpoint or os.environ.get("HF_ENDPOINT") or DEFAULT_HF_ENDPOINT).rstrip("/")
    repo = str(repo).strip("/")
    if repo.count("/") != 1 or ".." in repo:
        raise ValueError("repo must look like 'owner/name'")
    url = f"{endpoint}/api/models/{repo}/tree/{urllib.parse.quote(str(revision), safe='')}?recursive=true"
    _check_url(url, allow_http)
    headers = {"User-Agent": "wsbuilder-llm", "Accept": "application/json"}
    token = token or os.environ.get("HF_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        with _opener().open(urllib.request.Request(url, headers=headers), timeout=timeout) as response:
            entries = json.loads(response.read(32 * 1024 * 1024).decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise DownloadError(f"listing {repo} failed: HTTP {exc.code}") from exc
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise DownloadError(f"listing {repo} failed: {exc}") from exc
    files = []
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict) or entry.get("type") != "file":
            continue
        path = str(entry.get("path", ""))
        if suffix and not path.lower().endswith(str(suffix).lower()):
            continue
        lfs = entry.get("lfs") if isinstance(entry.get("lfs"), dict) else {}
        oid = str(lfs.get("oid") or "").lower()
        files.append(
            {
                "path": path,
                "size": int(lfs.get("size") or entry.get("size") or 0),
                "sha256": oid if _SHA256.match(oid) else None,
            }
        )
    return files


# -- the store --------------------------------------------------------------


class ModelRecord:
    """One stored model: its file and what is known about it."""

    __slots__ = ("id", "name", "filename", "format", "size", "sha256", "source", "created_at",
                 "summary", "root")

    def __init__(self, *, id, name, filename, format, size, sha256, source, created_at, summary, root):
        self.id = id
        self.name = name
        self.filename = filename
        self.format = format
        self.size = int(size)
        self.sha256 = sha256
        self.source = source
        self.created_at = created_at
        self.summary = dict(summary or {})
        self.root = root

    @property
    def directory(self):
        return os.path.join(self.root, self.id)

    @property
    def path(self):
        return os.path.join(self.directory, self.filename)

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "filename": self.filename,
            "format": self.format,
            "size": self.size,
            "sha256": self.sha256,
            "source": self.source,
            "created_at": self.created_at,
            "summary": self.summary,
        }

    describe = to_dict

    @classmethod
    def from_dict(cls, data, root):
        return cls(
            id=data["id"],
            name=data.get("name") or data["id"],
            filename=_safe_filename(data["filename"]),
            format=data.get("format", "unknown"),
            size=data.get("size", 0),
            sha256=data.get("sha256"),
            source=data.get("source"),
            created_at=data.get("created_at"),
            summary=data.get("summary"),
            root=root,
        )

    def __repr__(self):
        return f"<ModelRecord {self.id} {self.format} {self.size} bytes>"


class ModelStore:
    """A directory of model files, one sub-directory per model.

    ``root`` defaults to :func:`default_model_root`. Every model directory
    holds the NDJSON model file and a ``manifest.json``; there is no shared
    index to corrupt, and writes go through temporary files and
    ``os.replace``.

    :meth:`load` runs stored ``wsbuilder.bytelm`` models natively. A custom
    ``loader(record, **options)`` returning a :class:`LanguageModel` (or any
    object with its interface) replaces that for every model.
    """

    def __init__(self, root=None, *, loader=None):
        self.root = os.path.abspath(os.fspath(root)) if root is not None else default_model_root()
        os.makedirs(self.root, exist_ok=True)
        self.loader = loader
        self._lock = threading.RLock()
        self._loaded = {}

    # -- lookup --------------------------------------------------------

    def list(self):
        records = []
        for entry in sorted(os.listdir(self.root)):
            record = self._read_manifest(entry)
            if record is not None:
                records.append(record)
        return records

    def get(self, model_id):
        record = self._read_manifest(str(model_id))
        if record is None:
            raise KeyError(f"no model {model_id!r} in {self.root}")
        return record

    def __contains__(self, model_id):
        return self._read_manifest(str(model_id)) is not None

    def path(self, model_id):
        return self.get(model_id).path

    def _read_manifest(self, model_id):
        if not _SAFE_ID.match(model_id):
            return None
        manifest = os.path.join(self.root, model_id, _MANIFEST)
        try:
            with open(manifest, encoding="utf-8") as handle:
                data = json.load(handle)
            record = ModelRecord.from_dict(data, self.root)
        except (OSError, ValueError, KeyError, TypeError):
            return None
        if record.id != model_id or not os.path.isfile(record.path):
            return None
        return record

    def _write_manifest(self, record):
        manifest = os.path.join(record.directory, _MANIFEST)
        temporary = manifest + f".tmp-{uuid.uuid4().hex}"
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(record.to_dict(), handle, ensure_ascii=False, indent=2)
        os.replace(temporary, manifest)

    def _new_id(self, name, model_id=None):
        if model_id is not None:
            model_id = str(model_id)
            if not _SAFE_ID.match(model_id):
                raise ValueError("model ids use lowercase letters, digits, '.', '_' and '-'")
            if os.path.exists(os.path.join(self.root, model_id)):
                raise FileExistsError(f"model {model_id!r} already exists")
            return model_id
        base = _slug(name)
        candidate = base
        counter = 2
        while os.path.exists(os.path.join(self.root, candidate)):
            candidate = f"{base}-{counter}"
            counter += 1
        return candidate

    # -- adding --------------------------------------------------------

    def _register(self, staged_path, *, name, filename, source, model_id=None, sha256=None,
                  require_known_format=True):
        """Move a staged file into its model directory and write the manifest."""
        kind = detect_model_format(staged_path)
        if require_known_format and kind == "unknown":
            raise ValueError("not a wsbuilder NDJSON model file (pass require_known_format=False)")
        try:
            summary = summarize_model_file(staged_path)
        except (ModelFormatError, ValueError) as exc:
            raise ValueError(f"the model file is malformed: {exc}") from exc
        digest = sha256 or _sha256_of(staged_path)
        with self._lock:
            model_id = self._new_id(name or summary.get("name") or filename, model_id)
            directory = os.path.join(self.root, model_id)
            os.makedirs(directory)
            final = os.path.join(directory, filename)
            os.replace(staged_path, final)
            record = ModelRecord(
                id=model_id,
                name=name or summary.get("name") or os.path.splitext(filename)[0],
                filename=filename,
                format=kind,
                size=os.path.getsize(final),
                sha256=digest,
                source=source,
                created_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                summary=summary,
                root=self.root,
            )
            self._write_manifest(record)
        return record

    def _staging_path(self, filename):
        staging = os.path.join(self.root, ".staging")
        os.makedirs(staging, exist_ok=True)
        return os.path.join(staging, f"{uuid.uuid4().hex}-{filename}")

    def import_file(self, path, *, name=None, model_id=None, move=False, require_known_format=True):
        """Copy (or move) a model file from disk into the store."""
        source = os.path.abspath(os.fspath(path))
        if not os.path.isfile(source):
            raise FileNotFoundError(source)
        filename = _safe_filename(os.path.basename(source))
        staged = self._staging_path(filename)
        if move:
            shutil.move(source, staged)
        else:
            shutil.copyfile(source, staged)
        try:
            return self._register(
                staged, name=name, filename=filename, source=f"file:{source}", model_id=model_id,
                require_known_format=require_known_format,
            )
        except BaseException:
            if move and os.path.exists(staged):
                shutil.move(staged, source)
            elif os.path.exists(staged):
                os.unlink(staged)
            raise

    def download(self, url, *, name=None, model_id=None, filename=None, sha256=None, token=None,
                 progress=None, timeout=60.0, max_bytes=DEFAULT_MAX_DOWNLOAD_BYTES,
                 allow_http=False, cancel_event=None, require_known_format=True):
        """Download a model file from a URL into the store."""
        filename = _safe_filename(
            filename or os.path.basename(urllib.parse.urlsplit(str(url)).path) or "model.ndjson"
        )
        # A stable staging name per URL lets an interrupted download resume.
        key = hashlib.sha256(f"{url}|{sha256 or ''}".encode()).hexdigest()[:16]
        staging = os.path.join(self.root, ".staging")
        os.makedirs(staging, exist_ok=True)
        staged = os.path.join(staging, f"{key}-{filename}")
        digest = download_file(
            url, staged, sha256=sha256, token=token, progress=progress, timeout=timeout,
            max_bytes=max_bytes, allow_http=allow_http, cancel_event=cancel_event,
        )
        try:
            return self._register(
                staged, name=name, filename=filename, source=url, model_id=model_id, sha256=digest,
                require_known_format=require_known_format,
            )
        except BaseException:
            if os.path.exists(staged):
                os.unlink(staged)
            raise

    def download_huggingface(self, repo, filename, *, revision="main", name=None, model_id=None,
                             token=None, endpoint=None, sha256=None, verify=True, **options):
        """Download ``filename`` from a Hub repository, verified by SHA-256.

        With ``verify`` (the default) the expected SHA-256 is taken from the
        Hub's file listing when not given, and a mismatch deletes the file.
        """
        token = token or os.environ.get("HF_TOKEN")
        allow_http = options.get("allow_http", False)
        if verify and sha256 is None:
            listing = huggingface_files(
                repo, revision=revision, endpoint=endpoint, token=token, allow_http=allow_http
            )
            match = next((entry for entry in listing if entry["path"] == filename), None)
            if match is None:
                raise DownloadError(f"{filename!r} is not in {repo}@{revision}")
            sha256 = match["sha256"]
            if sha256 is None:
                raise DownloadError(f"the Hub publishes no SHA-256 for {filename!r}; pass verify=False")
        url = huggingface_url(repo, filename, revision=revision, endpoint=endpoint)
        record = self.download(
            url, name=name or f"{repo.split('/')[-1]}/{os.path.basename(filename)}",
            model_id=model_id, filename=os.path.basename(filename), sha256=sha256, token=token,
            **options,
        )
        record.source = f"hf:{repo}@{revision}/{filename}"
        self._write_manifest(record)
        return record

    # -- other operations ----------------------------------------------

    def export(self, model_id, destination):
        """Copy a stored model file out of the store; returns the new path."""
        record = self.get(model_id)
        target = os.fspath(destination)
        if os.path.isdir(target):
            target = os.path.join(target, record.filename)
        if os.path.exists(target):
            raise FileExistsError(target)
        temporary = f"{target}.tmp-{uuid.uuid4().hex}"
        shutil.copyfile(record.path, temporary)
        os.replace(temporary, target)
        return target

    def verify(self, model_id):
        """Recompute the SHA-256; True when the file is unchanged."""
        record = self.get(model_id)
        return record.sha256 is not None and _sha256_of(record.path) == record.sha256

    def rename(self, model_id, name):
        record = self.get(model_id)
        record.name = str(name)
        self._write_manifest(record)
        return record

    def remove(self, model_id):
        """Delete a model and its directory."""
        with self._lock:
            record = self.get(model_id)
            self.unload(model_id)
            shutil.rmtree(record.directory)
        return record

    def total_size(self):
        return sum(record.size for record in self.list())

    # -- running -------------------------------------------------------

    def open(self, model_id):
        """A :class:`~wsbuilder.ndjson_model.ModelReader` on a stored model."""
        return ModelReader(self.get(model_id).path)

    def load_network(self, model_id, **options):
        """A stored ``wsbuilder.dense`` model as a :class:`~wsbuilder.ia.NeuralNetwork`."""
        from .ia import NeuralNetwork

        return NeuralNetwork.load(self.get(model_id).path, **options)

    def load(self, model_id, **backend_options):
        """A :class:`LanguageModel` for a stored model.

        ``wsbuilder.bytelm`` models run natively; ``backend_options`` are
        their default generation options (``temperature``, ``max_tokens``...).
        Loaded models are cached per id; :meth:`unload` releases one.
        Raises ``NotImplementedError`` for a model no backend can run.
        """
        with self._lock:
            model = self._loaded.get(model_id)
            if model is not None:
                return model
            record = self.get(model_id)
            loader = self.loader or _native_loader
            model = loader(record, **backend_options)
            self._loaded[model_id] = model
            return model

    def add(self, model, *, name=None, model_id=None, dtype="F32"):
        """Save a model object (``ByteLanguageModel``, ``NeuralNetwork``) into the store."""
        if not hasattr(model, "to_model"):
            raise TypeError("model must provide to_model() (ByteLanguageModel or NeuralNetwork)")
        label = str(name or getattr(model, "architecture", None) or "model")
        filename = _safe_filename(f"{_slug(label)}.ndjson")
        staged = self._staging_path(filename)
        model.to_model(dtype=dtype, name=label).write(staged)
        try:
            return self._register(staged, name=label, filename=filename, source="wsbuilder",
                                  model_id=model_id)
        except BaseException:
            if os.path.exists(staged):
                os.unlink(staged)
            raise

    def unload(self, model_id):
        with self._lock:
            model = self._loaded.pop(model_id, None)
        _close(model)

    def close(self):
        with self._lock:
            loaded, self._loaded = self._loaded, {}
        for model in loaded.values():
            _close(model)


def _native_loader(record, **options):
    from .ia_lm import BYTELM_ARCHITECTURE, ByteLanguageModel, NativeBackend

    architecture = record.summary.get("architecture")
    if record.format != "ndjson" or architecture != BYTELM_ARCHITECTURE:
        raise NotImplementedError(
            f"{record.id!r} ({record.format}, architecture {architecture!r}) is not a language "
            f"model wsbuilder can run; train one with wsbuilder.ByteLanguageModel"
        )
    return LanguageModel(NativeBackend(ByteLanguageModel.load(record.path), **options), name=record.id)


def _close(model):
    closer = getattr(model, "close", None)
    if callable(closer):
        closer()


# -- backends ---------------------------------------------------------------


def _generation_options(options):
    allowed = ("max_tokens", "temperature", "top_p", "top_k", "stop", "seed", "repeat_penalty",
               "presence_penalty", "frequency_penalty")
    return {key: options[key] for key in allowed if options.get(key) is not None}


class OpenAICompatibleBackend:
    """Any server speaking the OpenAI chat API, through the standard library.

    Examples of ``base_url``: ``http://localhost:11434/v1`` (Ollama),
    ``http://localhost:8080/v1`` (llama.cpp server), ``https://api.openai.com/v1``.
    """

    name = "openai-compatible"

    def __init__(self, base_url, model, *, api_key=None, timeout=120.0, headers=None):
        parts = urllib.parse.urlsplit(str(base_url))
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise ValueError("base_url must be an http(s) URL")
        self.base_url = str(base_url).rstrip("/")
        self.model = str(model)
        self.api_key = api_key
        self.timeout = float(timeout)
        self.headers = dict(headers or {})

    def _post(self, path, body):
        headers = {"Content-Type": "application/json", "User-Agent": "wsbuilder-llm"}
        headers.update(self.headers)
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = urllib.request.Request(
            self.base_url + path, data=json.dumps(body).encode("utf-8"), headers=headers, method="POST"
        )
        try:
            return _opener().open(request, timeout=self.timeout)
        except urllib.error.HTTPError as exc:
            detail = exc.read(2000).decode("utf-8", "replace")
            raise RuntimeError(f"{self.base_url}{path}: HTTP {exc.code} {detail}") from exc
        except (urllib.error.URLError, OSError) as exc:
            raise ConnectionError(f"{self.base_url}{path}: {getattr(exc, 'reason', exc)}") from exc

    def complete(self, prompt, *, stream=False, **options):
        body = {"model": self.model, "prompt": str(prompt), **_generation_options(options)}
        if not stream:
            with self._post("/completions", body) as response:
                result = json.loads(response.read().decode("utf-8"))
            choice = result["choices"][0]
            return {"text": choice.get("text", ""), "finish_reason": choice.get("finish_reason"),
                    "usage": result.get("usage")}
        body["stream"] = True
        return self._events("/completions", body, lambda choice: choice.get("text"))

    def chat(self, messages, *, stream=False, **options):
        body = {"model": self.model, "messages": list(messages), **_generation_options(options)}
        if not stream:
            with self._post("/chat/completions", body) as response:
                result = json.loads(response.read().decode("utf-8"))
            choice = result["choices"][0]
            return {"text": (choice.get("message") or {}).get("content") or "",
                    "finish_reason": choice.get("finish_reason"), "usage": result.get("usage")}
        body["stream"] = True
        return self._events(
            "/chat/completions", body, lambda choice: (choice.get("delta") or {}).get("content")
        )

    def _events(self, path, body, pick):
        with self._post(path, body) as response:
            for raw_line in response:
                line = raw_line.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    return
                try:
                    event = json.loads(data)
                except ValueError:
                    continue
                choices = event.get("choices") or []
                if choices:
                    piece = pick(choices[0])
                    if piece:
                        yield piece

    def close(self):
        pass


class LanguageModel:
    """A model behind a backend, with one small interface for both kinds.

    ``generate(prompt)`` and ``chat(messages)`` return the text;
    ``stream_generate`` and ``stream_chat`` yield it piece by piece;
    ``chat_completion`` returns the backend's text, finish reason and usage.
    """

    def __init__(self, backend, *, name=None):
        self.backend = backend
        self.name = name or getattr(backend, "model", None) or getattr(backend, "name", "model")

    @classmethod
    def from_file(cls, path, **defaults):
        """A native model read from a ``wsbuilder.bytelm`` NDJSON file."""
        from .ia_lm import NativeBackend

        return cls(NativeBackend(os.fspath(path), **defaults), name=os.path.basename(os.fspath(path)))

    @classmethod
    def native(cls, model, *, name=None, **defaults):
        """A :class:`~wsbuilder.ia_lm.ByteLanguageModel` object as a language model."""
        from .ia_lm import NativeBackend

        return cls(NativeBackend(model, **defaults), name=name or "native")

    @classmethod
    def from_openai(cls, base_url, model, **options):
        return cls(OpenAICompatibleBackend(base_url, model, **options), name=model)

    @staticmethod
    def _messages(messages):
        if isinstance(messages, str):
            return [{"role": "user", "content": messages}]
        out = []
        for message in messages:
            role = str(message.get("role", ""))
            if role not in ("system", "user", "assistant", "tool"):
                raise ValueError(f"unknown message role {role!r}")
            out.append({"role": role, "content": str(message.get("content", ""))})
        return out

    def generate(self, prompt, **options):
        return self.backend.complete(str(prompt), **options)["text"]

    def stream_generate(self, prompt, **options):
        return self.backend.complete(str(prompt), stream=True, **options)

    def chat(self, messages, **options):
        return self.chat_completion(messages, **options)["text"]

    def chat_completion(self, messages, **options):
        return self.backend.chat(self._messages(messages), **options)

    def stream_chat(self, messages, **options):
        return self.backend.chat(self._messages(messages), stream=True, **options)

    def close(self):
        self.backend.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


# -- HTTP -------------------------------------------------------------------

#: Caps on what ``POST train`` accepts; training runs in pure Python on the
#: server's CPU, so keep them modest.
DEFAULT_TRAIN_LIMITS = {
    "max_text_bytes": 1024 * 1024,
    "max_context": 128,
    "max_embedding": 64,
    "max_hidden": 512,
    "max_layers": 4,
    "max_epochs": 500,
}


def _training_spec(payload, limits):
    """Validate a ``POST train`` body against ``limits``."""

    def integer(key, default, cap):
        value = payload.get(key, default)
        if type(value) is not int or not 1 <= value <= cap:
            raise ValueError(f"{key} must be an integer between 1 and {cap}")
        return value

    texts = payload.get("texts")
    conversations = payload.get("conversations")
    if (texts is None) == (conversations is None):
        raise ValueError("send either texts or conversations")
    if texts is not None:
        if isinstance(texts, str):
            texts = [texts]
        if not isinstance(texts, list) or not all(isinstance(text, str) for text in texts):
            raise ValueError("texts must be a list of strings")
        size = sum(len(text.encode("utf-8")) for text in texts)
    else:
        if not isinstance(conversations, list) or not all(
            isinstance(conversation, list) and all(isinstance(m, dict) for m in conversation)
            for conversation in conversations
        ):
            raise ValueError("conversations must be lists of {role, content} messages")
        size = sum(len(str(m.get("content", "")).encode("utf-8")) + 16
                   for conversation in conversations for m in conversation)
    if size > limits["max_text_bytes"]:
        raise ValueError(f"the training text exceeds {limits['max_text_bytes']} bytes")
    hidden = payload.get("hidden", [96])
    if isinstance(hidden, int):
        hidden = [hidden]
    if (not isinstance(hidden, list) or not 1 <= len(hidden) <= limits["max_layers"]
            or not all(type(h) is int and 1 <= h <= limits["max_hidden"] for h in hidden)):
        raise ValueError(f"hidden must list 1 to {limits['max_layers']} sizes up to {limits['max_hidden']}")
    rate = payload.get("learning_rate", 0.05)
    if isinstance(rate, bool) or not isinstance(rate, (int, float)) or not 0 < rate <= 10:
        raise ValueError("learning_rate must be a number in (0, 10]")
    seed = payload.get("seed")
    if seed is not None and type(seed) is not int:
        raise ValueError("seed must be an integer")
    return {
        "name": str(payload.get("name") or "native-model")[:100],
        "texts": texts,
        "conversations": conversations,
        "context": integer("context", 24, limits["max_context"]),
        "embedding": integer("embedding", 12, limits["max_embedding"]),
        "hidden": hidden,
        "epochs": integer("epochs", 10, limits["max_epochs"]),
        "learning_rate": float(rate),
        "seed": seed,
    }



def install_llm(app, store=None, *, prefix="/api/llm", models=None, allow_download=False,
                allow_import=False, allow_delete=False, allow_train=False, openai_path="/v1",
                backend_options=None, download_hosts=None, train_limits=None):
    """Serve a :class:`ModelStore` and its models from ``app``.

    Routes under ``prefix``: ``GET models``, ``GET model?id=``, ``POST chat``
    (``{"model", "messages", "stream"}``; streaming answers as NDJSON),
    ``POST generate``, and when allowed ``POST download``, ``POST import``,
    ``POST delete``, ``POST train`` and ``GET tasks?id=``. With ``openai_path`` (``"/v1"``),
    ``POST /v1/chat/completions`` and ``GET /v1/models`` speak the OpenAI API
    so existing clients can point at the app.

    ``models`` maps extra names to ready :class:`LanguageModel` objects (for
    example one backed by Ollama). Downloading, importing, deleting and
    training change the server's disk (and training uses its CPU), so they
    are off by default; ``download_hosts`` limits downloads to the listed
    hosts and ``train_limits`` overrides the caps in ``DEFAULT_TRAIN_LIMITS``. Protect these routes with
    ``app.enable_security`` before exposing them.
    """
    from .http import Response

    store = store if store is not None else ModelStore()
    extra = dict(models or {})
    backend_options = dict(backend_options or {})
    hosts = {str(host).lower() for host in (download_hosts or ())}
    base = "/" + str(prefix).strip("/")

    def model_for(name):
        if name in extra:
            return extra[name]
        if name in store:
            return store.load(name, **backend_options)
        raise KeyError(name)

    def error(status, message):
        return Response.json({"error": {"message": message}}, status=status)

    def body_of(request):
        payload = request.json()
        if not isinstance(payload, dict):
            raise ValueError("send a JSON object")
        return payload

    def run(payload):
        name = str(payload.get("model") or "")
        try:
            model = model_for(name)
        except KeyError:
            return None, error(404, f"unknown model {name!r}")
        except (ImportError, NotImplementedError) as exc:
            return None, error(501, str(exc))
        options = {key: payload[key] for key in ("max_tokens", "temperature", "top_p", "top_k",
                                                 "stop", "seed") if key in payload}
        return (model, options), None

    @app.api(f"{base}/models", methods=("GET",))
    def list_models(_request):
        stored = [record.to_dict() for record in store.list()]
        for record in stored:
            record["summary"].pop("chat_template", None)
        return {"models": stored, "remote": sorted(extra)}

    @app.api(f"{base}/model", methods=("GET",))
    def model_info(request):
        try:
            return store.get(request.query.get("id", "")).to_dict()
        except KeyError:
            return error(404, "unknown model")

    @app.api(f"{base}/chat", methods=("POST",))
    def chat(request):
        try:
            payload = body_of(request)
            prepared, failure = run(payload)
            if failure is not None:
                return failure
            model, options = prepared
            messages = payload.get("messages") or []
            if payload.get("stream"):
                pieces = model.stream_chat(messages, **options)
                return Response.stream(
                    (json.dumps({"content": piece}, ensure_ascii=False) + "\n" for piece in pieces),
                    content_type="application/x-ndjson",
                )
            result = model.chat_completion(messages, **options)
            return {"model": payload["model"], **result}
        except ValueError as exc:
            return error(400, str(exc))

    @app.api(f"{base}/generate", methods=("POST",))
    def generate(request):
        try:
            payload = body_of(request)
            prepared, failure = run(payload)
            if failure is not None:
                return failure
            model, options = prepared
            prompt = str(payload.get("prompt") or "")
            if payload.get("stream"):
                return Response.stream(
                    (json.dumps({"content": piece}, ensure_ascii=False) + "\n"
                     for piece in model.stream_generate(prompt, **options)),
                    content_type="application/x-ndjson",
                )
            return {"model": payload["model"], **model.backend.complete(prompt, **options)}
        except ValueError as exc:
            return error(400, str(exc))

    if allow_download:

        @app.api(f"{base}/download", methods=("POST",))
        def download(request):
            try:
                payload = body_of(request)
            except ValueError as exc:
                return error(400, str(exc))
            if payload.get("repo"):
                url = huggingface_url(payload["repo"], str(payload.get("filename") or ""),
                                      revision=str(payload.get("revision") or "main"))
            else:
                url = str(payload.get("url") or "")
            host = (urllib.parse.urlsplit(url).hostname or "").lower()
            if hosts and host not in hosts:
                return error(403, f"downloads from {host or 'that URL'} are not allowed")
            if urllib.parse.urlsplit(url).scheme != "https":
                return error(400, "only https:// downloads are allowed")
            progress = {"done": 0, "total": None}

            def job(handle):
                def report(done, total):
                    progress["done"], progress["total"] = done, total
                    handle.metadata["progress"] = dict(progress)

                if payload.get("repo"):
                    record = store.download_huggingface(
                        payload["repo"], str(payload.get("filename")),
                        revision=str(payload.get("revision") or "main"), name=payload.get("name"),
                        progress=report, cancel_event=handle.cancel_event,
                    )
                else:
                    record = store.download(
                        url, name=payload.get("name"), sha256=payload.get("sha256"),
                        progress=report, cancel_event=handle.cancel_event,
                    )
                return record.to_dict()

            task = app.tasks.spawn(job, name="llm-download", group="llm", pass_handle=True,
                                   metadata={"url": url})
            return Response.json({"task": task.id}, status=202)

    if allow_train:
        limits = dict(DEFAULT_TRAIN_LIMITS)
        limits.update(train_limits or {})

        @app.api(f"{base}/train", methods=("POST",))
        def train(request):
            from .ia_lm import ByteLanguageModel

            try:
                payload = body_of(request)
                spec = _training_spec(payload, limits)
                model = ByteLanguageModel(context=spec["context"], embedding=spec["embedding"],
                                          hidden=spec["hidden"], seed=spec["seed"])
            except (ValueError, TypeError) as exc:
                return error(400, str(exc))

            def job(handle):
                def report(epoch, loss):
                    handle.metadata["epoch"], handle.metadata["loss"] = epoch, loss

                options = {"epochs": spec["epochs"], "learning_rate": spec["learning_rate"],
                           "seed": spec["seed"], "progress": report, "task_handle": handle}
                if spec["conversations"] is not None:
                    model.fit_chat(spec["conversations"], **options)
                else:
                    model.fit(spec["texts"], **options)
                return store.add(model, name=spec["name"]).to_dict()

            task = app.tasks.spawn(job, name="llm-train", group="llm", pass_handle=True,
                                   metadata={"name": spec["name"], "epochs": spec["epochs"]})
            return Response.json({"task": task.id}, status=202)

    if allow_download or allow_train:

        @app.api(f"{base}/tasks", methods=("GET",))
        def task_status(request):
            task = app.tasks.get(request.query.get("id", ""))
            if task is None:
                return error(404, "unknown task")
            snapshot = task.snapshot()
            if task.status == "completed":
                snapshot["result"] = task.result
            elif task.exception is not None:
                snapshot["error"] = str(task.exception)
            return snapshot

    if allow_import:

        @app.api(f"{base}/import", methods=("POST",))
        def import_model(request):
            try:
                payload = body_of(request)
                record = store.import_file(str(payload.get("path") or ""), name=payload.get("name"))
            except (ValueError, FileNotFoundError, FileExistsError) as exc:
                return error(400, str(exc))
            return Response.json(record.to_dict(), status=201)

    if allow_delete:

        @app.api(f"{base}/delete", methods=("POST",))
        def delete_model(request):
            try:
                record = store.remove(str(body_of(request).get("id") or ""))
            except KeyError:
                return error(404, "unknown model")
            except ValueError as exc:
                return error(400, str(exc))
            return {"deleted": record.id}

    if openai_path:
        openai_base = "/" + str(openai_path).strip("/")

        @app.api(f"{openai_base}/models", methods=("GET",))
        def openai_models(_request):
            names = [record.id for record in store.list()] + sorted(extra)
            return {"object": "list",
                    "data": [{"id": name, "object": "model", "owned_by": "wsbuilder"} for name in names]}

        @app.api(f"{openai_base}/chat/completions", methods=("POST",))
        def openai_chat(request):
            try:
                payload = body_of(request)
                prepared, failure = run(payload)
                if failure is not None:
                    return failure
                model, options = prepared
                messages = payload.get("messages") or []
                created = int(time.time())
                completion_id = f"chatcmpl-{uuid.uuid4().hex}"
                name = payload["model"]
                if payload.get("stream"):

                    def events():
                        for piece in model.stream_chat(messages, **options):
                            chunk = {"id": completion_id, "object": "chat.completion.chunk",
                                     "created": created, "model": name,
                                     "choices": [{"index": 0, "delta": {"content": piece},
                                                  "finish_reason": None}]}
                            yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
                        last = {"id": completion_id, "object": "chat.completion.chunk",
                                "created": created, "model": name,
                                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
                        yield f"data: {json.dumps(last)}\n\n"
                        yield "data: [DONE]\n\n"

                    return Response.stream(events(), content_type="text/event-stream",
                                           headers={"Cache-Control": "no-cache"})
                result = model.chat_completion(messages, **options)
                return {"id": completion_id, "object": "chat.completion", "created": created,
                        "model": name,
                        "choices": [{"index": 0,
                                     "message": {"role": "assistant", "content": result["text"]},
                                     "finish_reason": result.get("finish_reason") or "stop"}],
                        "usage": result.get("usage")}
            except ValueError as exc:
                return error(400, str(exc))

    app.llm_store = store
    return store
