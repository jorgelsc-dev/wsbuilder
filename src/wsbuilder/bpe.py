"""Byte-level BPE tokenizer compatible with GPT-2, Qwen2/Qwen3 and Llama 3, stdlib only.

``BPETokenizer`` reads Hugging Face ``tokenizer.json`` files and GGUF tokenizer
metadata. The split regexes of those tokenizers use ``\\p{L}``/``\\p{N}``, which
the standard ``re`` module lacks, so they are implemented as hand-written
scanners that follow the regex alternation order and backtracking exactly.
``\\s`` follows the Unicode ``White_Space`` property used by Hugging Face, which
differs from ``str.isspace()`` only on U+001C..U+001F.
"""

import codecs
import functools
import heapq
import json
import re
import unicodedata
from collections.abc import Mapping
from pathlib import Path

__all__ = [
    "BPEStreamDecoder",
    "BPETokenizer",
    "GPT2_PATTERN",
    "LLAMA3_PATTERN",
    "PRETOKENIZERS",
    "QWEN2_PATTERN",
    "bytes_to_unicode",
    "format_qwen3_chat",
    "pretokenize_gpt2",
    "pretokenize_llama3",
    "pretokenize_qwen2",
]

QWEN2_PATTERN = (
    r"(?i:'s|'t|'re|'ve|'m|'ll|'d)|[^\r\n\p{L}\p{N}]?\p{L}+|\p{N}"
    r"| ?[^\s\p{L}\p{N}]+[\r\n]*|\s*[\r\n]+|\s+(?!\S)|\s+"
)
LLAMA3_PATTERN = (
    r"(?i:'s|'t|'re|'ve|'m|'ll|'d)|[^\r\n\p{L}\p{N}]?\p{L}+|\p{N}{1,3}"
    r"| ?[^\s\p{L}\p{N}]+[\r\n]*|\s*[\r\n]+|\s+(?!\S)|\s+"
)
GPT2_PATTERN = r"'s|'t|'re|'ve|'m|'ll|'d| ?\p{L}+| ?\p{N}+| ?[^\s\p{L}\p{N}]+|\s+(?!\S)|\s+"

_NORMALIZATION_FORMS = ("NFC", "NFD", "NFKC", "NFKD")
_LETTER, _NUMBER, _SPACE, _OTHER = range(4)
# str.isspace() is True for these separators; Unicode White_Space (and \s in
# the Hugging Face regex engine) is not.
_NOT_WHITE_SPACE = frozenset("\x1c\x1d\x1e\x1f")
_NEWLINES = "\r\n"
_CONTRACTION = re.compile(r"'s|'t|'re|'ve|'m|'ll|'d")
_CONTRACTION_IGNORECASE = re.compile(r"(?i:'s|'t|'re|'ve|'m|'ll|'d)")


@functools.cache
def bytes_to_unicode():
    """GPT-2 table mapping each byte (0..255) to a printable Unicode character."""
    printable = (
        list(range(ord("!"), ord("~") + 1))
        + list(range(ord("\xa1"), ord("\xac") + 1))
        + list(range(ord("\xae"), ord("\xff") + 1))
    )
    codes = printable[:]
    extra = 0
    for byte in range(256):
        if byte not in printable:
            printable.append(byte)
            codes.append(256 + extra)
            extra += 1
    return dict(zip(printable, map(chr, codes), strict=True))


_BYTE_ENCODER = bytes_to_unicode()
_BYTE_DECODER = {char: byte for byte, char in _BYTE_ENCODER.items()}


def _is_white_space(char):
    return char.isspace() and char not in _NOT_WHITE_SPACE


@functools.lru_cache(maxsize=65536)
def _char_class(char):
    category = unicodedata.category(char)[0]
    if category == "L":
        return _LETTER
    if category == "N":
        return _NUMBER
    if _is_white_space(char):
        return _SPACE
    return _OTHER


def _run(text, index, end, kind):
    while index < end and _char_class(text[index]) == kind:
        index += 1
    return index


def _match_whitespace(text, index, end, newline_alternative):
    stop = _run(text, index + 1, end, _SPACE)
    if newline_alternative:
        # \s*[\r\n]+ : backtracks to the last newline of the whitespace run.
        for last in range(stop - 1, index - 1, -1):
            if text[last] in _NEWLINES:
                return last + 1
    # \s+(?!\S) : at end of text keeps the run, before a non-space gives back
    # one character, and needs at least one; otherwise \s+ takes the run.
    if stop == end or stop - 1 == index:
        return stop
    return stop - 1


def _match_llama_style(text, index, end, max_digits):
    char = text[index]
    if char == "'":
        match = _CONTRACTION_IGNORECASE.match(text, index)
        if match:
            return match.end()
    kind = _char_class(char)
    # [^\r\n\p{L}\p{N}]?\p{L}+
    if kind == _LETTER:
        return _run(text, index + 1, end, _LETTER)
    if (char not in _NEWLINES and kind != _NUMBER and index + 1 < end
            and _char_class(text[index + 1]) == _LETTER):
        return _run(text, index + 2, end, _LETTER)
    # \p{N} or \p{N}{1,3}
    if kind == _NUMBER:
        return _run(text, index + 1, min(end, index + max_digits), _NUMBER)
    # ' ?[^\s\p{L}\p{N}]+[\r\n]*'
    start = None
    if kind == _OTHER:
        start = index
    elif char == " " and index + 1 < end and _char_class(text[index + 1]) == _OTHER:
        start = index + 1
    if start is not None:
        stop = _run(text, start + 1, end, _OTHER)
        while stop < end and text[stop] in _NEWLINES:
            stop += 1
        return stop
    return _match_whitespace(text, index, end, True)


def _match_gpt2(text, index, end):
    char = text[index]
    if char == "'":
        match = _CONTRACTION.match(text, index)
        if match:
            return match.end()
    kind = _char_class(char)
    if kind == _SPACE:
        if char == " " and index + 1 < end:
            following = _char_class(text[index + 1])
            if following != _SPACE:
                # ' ?\p{L}+', ' ?\p{N}+' and ' ?[^\s\p{L}\p{N}]+'
                return _run(text, index + 2, end, following)
        return _match_whitespace(text, index, end, False)
    return _run(text, index + 1, end, kind)


def _scan(text, matcher, *args):
    if not isinstance(text, str):
        raise TypeError("text must be a string")
    pieces = []
    index = 0
    end = len(text)
    while index < end:
        stop = matcher(text, index, end, *args)
        pieces.append(text[index:stop])
        index = stop
    return pieces


def pretokenize_qwen2(text):
    """Split text like the Qwen2/Qwen3 regex (``QWEN2_PATTERN``), digit by digit."""
    return _scan(text, _match_llama_style, 1)


def pretokenize_llama3(text):
    """Split text like the Llama 3 regex (``LLAMA3_PATTERN``), up to 3 digits."""
    return _scan(text, _match_llama_style, 3)


def pretokenize_gpt2(text):
    """Split text like the GPT-2 regex (``GPT2_PATTERN``)."""
    return _scan(text, _match_gpt2)


PRETOKENIZERS = {
    "qwen2": pretokenize_qwen2,
    "llama3": pretokenize_llama3,
    "gpt2": pretokenize_gpt2,
}
_PATTERN_NAMES = {QWEN2_PATTERN: "qwen2", LLAMA3_PATTERN: "llama3", GPT2_PATTERN: "gpt2"}
_GGUF_PRE_NAMES = {
    "qwen2": "qwen2",
    "llama-bpe": "llama3",
    "llama3": "llama3",
    "gpt-2": "gpt2",
    "gpt2": "gpt2",
}


class _AddedToken:
    __slots__ = ("id", "content", "special", "lstrip", "rstrip", "normalized", "single_word")

    def __init__(self, spec):
        if not isinstance(spec, Mapping):
            raise TypeError("added tokens must be mappings")
        token_id = spec.get("id")
        content = spec.get("content")
        if type(token_id) is not int or token_id < 0:
            raise ValueError("added token id must be a non-negative integer")
        if not isinstance(content, str) or not content:
            raise ValueError("added token content must be a non-empty string")
        self.id = token_id
        self.content = content
        self.special = bool(spec.get("special", False))
        self.lstrip = bool(spec.get("lstrip", False))
        self.rstrip = bool(spec.get("rstrip", False))
        # Hugging Face defaults to normalized=True only for non-special tokens.
        self.normalized = bool(spec.get("normalized", not self.special))
        self.single_word = bool(spec.get("single_word", False))


def _token_matcher(tokens):
    if not tokens:
        return None
    # Longest alternatives first: at the leftmost position, the longest wins.
    contents = sorted((token.content for token in tokens), key=len, reverse=True)
    return re.compile("|".join(map(re.escape, contents)))


def _is_word_char(char):
    return char.isalnum() or char == "_"


def _split_added(text, matcher, by_content):
    pieces = []
    position = search_from = 0
    end = len(text)
    while search_from <= end:
        match = matcher.search(text, search_from)
        if match is None:
            break
        token = by_content[match.group()]
        start, stop = match.span()
        if token.single_word and (
            (start > 0 and _is_word_char(text[start - 1]))
            or (stop < end and _is_word_char(text[stop]))
        ):
            search_from = start + 1
            continue
        if token.lstrip:
            while start > position and _is_white_space(text[start - 1]):
                start -= 1
        if token.rstrip:
            while stop < end and _is_white_space(text[stop]):
                stop += 1
        if start > position:
            pieces.append(text[position:start])
        pieces.append(token.id)
        position = search_from = stop
    if position < end:
        pieces.append(text[position:])
    return pieces


def _normalizer_forms(normalizer):
    if normalizer is None:
        return ()
    forms = (normalizer,) if isinstance(normalizer, str) else tuple(normalizer)
    for form in forms:
        if form not in _NORMALIZATION_FORMS:
            raise ValueError(f"unsupported normalizer {form!r}; use one of {_NORMALIZATION_FORMS}")
    return forms


def _parse_normalizer(spec):
    if spec is None:
        return ()
    if not isinstance(spec, Mapping):
        raise ValueError("tokenizer.json normalizer must be an object or null")
    kind = spec.get("type")
    if kind in _NORMALIZATION_FORMS:
        return (kind,)
    if kind == "Sequence":
        forms = ()
        for item in spec.get("normalizers") or ():
            forms += _parse_normalizer(item)
        return forms
    raise ValueError(f"unsupported normalizer type {kind!r}; supported: NFC, NFD, NFKC, NFKD")


def _parse_pre_tokenizer(spec, explicit):
    if not isinstance(spec, Mapping):
        raise ValueError("tokenizer.json needs a ByteLevel pre_tokenizer for byte-level BPE")
    steps = spec.get("pretokenizers") if spec.get("type") == "Sequence" else [spec]
    splits = []
    byte_level = None
    for step in steps or ():
        kind = step.get("type") if isinstance(step, Mapping) else None
        if kind == "ByteLevel":
            if byte_level is not None:
                raise ValueError("pre_tokenizer has more than one ByteLevel step")
            byte_level = step
        elif kind == "Split":
            splits.append(step)
        elif explicit is None:
            raise ValueError(f"unsupported pre_tokenizer type {kind!r}")
    if byte_level is None:
        raise ValueError("tokenizer.json needs a ByteLevel pre_tokenizer for byte-level BPE")
    add_prefix_space = bool(byte_level.get("add_prefix_space", False))
    if explicit is not None:
        return explicit, add_prefix_space
    patterns = [GPT2_PATTERN] if byte_level.get("use_regex", True) else []
    for split in splits:
        if split.get("behavior") != "Isolated" or split.get("invert", False):
            raise ValueError("only Split pre_tokenizers with behavior 'Isolated' are supported")
        pattern = split.get("pattern")
        if not isinstance(pattern, Mapping) or not isinstance(pattern.get("Regex"), str):
            raise ValueError("Split pre_tokenizer needs a Regex pattern")
        patterns.append(pattern["Regex"])
    if not patterns:
        return _no_split, add_prefix_space
    if len(patterns) > 1:
        raise ValueError("several split patterns are not supported; pass pretokenizer=")
    name = _PATTERN_NAMES.get(patterns[0])
    if name is None:
        raise ValueError(
            f"unknown pre-tokenizer pattern {patterns[0]!r}; known: Qwen2/Qwen3, Llama 3 "
            "and GPT-2. Pass pretokenizer= with a name or a callable to override"
        )
    return name, add_prefix_space


def _no_split(text):
    return [text]


def _load_json(data):
    if isinstance(data, (bytes, bytearray)):
        data = bytes(data).decode("utf-8")
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except ValueError as error:
            raise ValueError(f"invalid tokenizer JSON: {error}") from None
    if not isinstance(data, Mapping):
        raise ValueError("tokenizer JSON must be an object")
    return data


def _parse_merges(merges):
    if isinstance(merges, (str, bytes, Mapping)) or not hasattr(merges, "__iter__"):
        raise ValueError("merges must be a list of 'a b' strings or [a, b] pairs")
    pairs = []
    for merge in merges:
        if isinstance(merge, str):
            parts = merge.split(" ")
        elif isinstance(merge, (list, tuple)):
            parts = list(merge)
        else:
            raise ValueError(f"invalid merge {merge!r}")
        if len(parts) != 2 or not all(isinstance(part, str) and part for part in parts):
            raise ValueError(f"invalid merge {merge!r}; expected 'a b' or [a, b]")
        pairs.append((parts[0], parts[1]))
    return pairs


def _merge_word(word, ranks):
    """Apply merges by lowest (rank, position), like Hugging Face: O(n log n)."""
    symbols = list(word)
    count = len(symbols)
    if count < 2:
        return symbols
    following = list(range(1, count)) + [-1]
    previous = list(range(-1, count - 1))
    heap = []
    for index in range(count - 1):
        rank = ranks.get((symbols[index], symbols[index + 1]))
        if rank is not None:
            heap.append((rank, index, symbols[index], symbols[index + 1]))
    heapq.heapify(heap)
    while heap:
        _, index, first, second = heapq.heappop(heap)
        right = following[index]
        # Symbols only grow and removed ones become None, so an entry is
        # current exactly when both strings are still in place.
        if symbols[index] != first or right < 0 or symbols[right] != second:
            continue
        merged = first + second
        symbols[index] = merged
        symbols[right] = None
        after = following[right]
        following[index] = after
        if after >= 0:
            previous[after] = index
            rank = ranks.get((merged, symbols[after]))
            if rank is not None:
                heapq.heappush(heap, (rank, index, merged, symbols[after]))
        before = previous[index]
        if before >= 0:
            rank = ranks.get((symbols[before], merged))
            if rank is not None:
                heapq.heappush(heap, (rank, before, symbols[before], merged))
    return [symbol for symbol in symbols if symbol is not None]


def _token_text(value, name):
    if isinstance(value, Mapping):
        value = value.get("content")
    if value is not None and not isinstance(value, str):
        raise ValueError(f"{name} in tokenizer_config.json must be a string or null")
    return value


class BPETokenizer:
    """Byte-level BPE tokenizer (GPT-2/Qwen2/Qwen3/Llama 3 style).

    Encoding splits added/special tokens by leftmost-longest match, normalizes,
    pre-tokenizes, maps UTF-8 bytes to the GPT-2 byte alphabet and applies the
    merges by rank. ``vocab_size`` is the highest token id plus one.
    """

    def __init__(self, vocab, merges, *, added_tokens=(), pretokenizer="qwen2",
                 normalizer=None, add_prefix_space=False, ignore_merges=False,
                 bos_token_id=None, eos_token_id=None, pad_token_id=None,
                 add_bos_token=False, add_eos_token=False, unused_token_ids=(),
                 cache_size=10_000):
        if not isinstance(vocab, Mapping):
            raise TypeError("vocab must be a mapping of token -> id")
        if type(cache_size) is not int or cache_size < 0:
            raise ValueError("cache_size must be a non-negative integer")
        self._vocab = {}
        self._id_to_token = {}
        for token, token_id in vocab.items():
            if not isinstance(token, str) or not token:
                raise ValueError("vocab tokens must be non-empty strings")
            if type(token_id) is not int or token_id < 0:
                raise ValueError(f"vocab id for {token!r} must be a non-negative integer")
            if token_id in self._id_to_token:
                raise ValueError(f"vocab id {token_id} is assigned to more than one token")
            self._vocab[token] = token_id
            self._id_to_token[token_id] = token
        self._ranks = {}
        for rank, (first, second) in enumerate(_parse_merges(merges)):
            for part in (first, second, first + second):
                if part not in self._vocab:
                    raise ValueError(f"merge {first!r} {second!r}: token {part!r} not in vocab")
            self._ranks.setdefault((first, second), rank)
        # Unused/padding ids (GGUF type 5): never produced and decoded as nothing.
        self._unused = set()
        for token_id in unused_token_ids:
            token = self._id_to_token.get(token_id) if type(token_id) is int else None
            if token is None:
                raise ValueError(f"unused token id {token_id!r} is not in the vocab")
            self._vocab.pop(token, None)
            self._unused.add(token_id)
        self._added = {}
        by_content = {}
        for spec in added_tokens:
            token = _AddedToken(spec)
            if token.content in by_content:
                raise ValueError(f"duplicate added token {token.content!r}")
            if token.id in self._added:
                raise ValueError(f"added token id {token.id} is used more than once")
            by_content[token.content] = token
            self._added[token.id] = token
        self._added_by_content = by_content
        self._matchers = {}
        for allow_special in (True, False):
            tokens = [t for t in by_content.values() if allow_special or not t.special]
            self._matchers[allow_special] = (
                _token_matcher([t for t in tokens if not t.normalized]),
                _token_matcher([t for t in tokens if t.normalized]),
            )
        self._pretokenizer_name = pretokenizer if isinstance(pretokenizer, str) else next(
            (name for name, func in PRETOKENIZERS.items() if func is pretokenizer),
            "none" if pretokenizer is _no_split else None,
        )
        if pretokenizer == "none":
            pretokenizer = _no_split
        if isinstance(pretokenizer, str):
            if pretokenizer not in PRETOKENIZERS:
                raise ValueError(
                    f"unknown pretokenizer {pretokenizer!r}; use one of {sorted(PRETOKENIZERS)}"
                )
            pretokenizer = PRETOKENIZERS[pretokenizer]
        if not callable(pretokenizer):
            raise TypeError("pretokenizer must be a name or a callable returning strings")
        self._pretokenize = pretokenizer
        self._normalizer = _normalizer_forms(normalizer)
        self.add_prefix_space = bool(add_prefix_space)
        self.ignore_merges = bool(ignore_merges)
        all_ids = list(self._id_to_token) + list(self._added)
        self._vocab_size = max(all_ids) + 1 if all_ids else 0
        self.bos_token_id = self._check_id(bos_token_id, "bos_token_id")
        self.eos_token_id = self._check_id(eos_token_id, "eos_token_id")
        self.pad_token_id = self._check_id(pad_token_id, "pad_token_id")
        self.add_bos_token = bool(add_bos_token)
        self.add_eos_token = bool(add_eos_token)
        if self.add_bos_token and self.bos_token_id is None:
            raise ValueError("add_bos_token requires bos_token_id")
        if self.add_eos_token and self.eos_token_id is None:
            raise ValueError("add_eos_token requires eos_token_id")
        self._cache_size = cache_size
        self._cache = {}

    # -- construction -----------------------------------------------------

    @classmethod
    def from_json(cls, data, *, pretokenizer=None, **options):
        """Build from a Hugging Face ``tokenizer.json`` object (dict or JSON text)."""
        data = _load_json(data)
        model = data.get("model")
        if not isinstance(model, Mapping):
            raise ValueError("tokenizer JSON has no model object")
        kind = model.get("type", "BPE" if "merges" in model else None)
        if kind != "BPE":
            raise ValueError(f"unsupported tokenizer model {kind!r}; only BPE is supported")
        if model.get("dropout") not in (None, 0, 0.0):
            raise ValueError("BPE dropout is not supported")
        if model.get("continuing_subword_prefix") or model.get("end_of_word_suffix"):
            raise ValueError("BPE subword prefixes/suffixes are not supported (not byte-level)")
        if model.get("byte_fallback"):
            raise ValueError("BPE byte_fallback is not supported (not byte-level)")
        vocab = model.get("vocab")
        if not isinstance(vocab, Mapping):
            raise ValueError("tokenizer JSON model.vocab must be an object")
        merges = model.get("merges")
        merges = [] if merges is None else merges
        if not isinstance(merges, list):
            raise ValueError("tokenizer JSON model.merges must be a list")
        added = data.get("added_tokens") or []
        if not isinstance(added, list):
            raise ValueError("tokenizer JSON added_tokens must be a list")
        split, add_prefix_space = _parse_pre_tokenizer(data.get("pre_tokenizer"), pretokenizer)
        options.setdefault("add_prefix_space", add_prefix_space)
        extension = data.get("wsbuilder") or {}
        if not isinstance(extension, Mapping):
            raise ValueError("tokenizer JSON wsbuilder must be an object")
        options.setdefault("unused_token_ids", extension.get("unused_token_ids") or ())
        options.setdefault("normalizer", _parse_normalizer(data.get("normalizer")))
        options.setdefault("ignore_merges", bool(model.get("ignore_merges", False)))
        return cls(vocab, merges, added_tokens=added, pretokenizer=split, **options)

    @classmethod
    def from_file(cls, path, **options):
        """Build from a ``tokenizer.json`` file."""
        return cls.from_json(Path(path).read_bytes(), **options)

    @classmethod
    def from_pretrained_dir(cls, path, **options):
        """Build from a model directory: ``tokenizer.json`` plus ``tokenizer_config.json``.

        The config, when present, provides ``bos_token``/``eos_token``/``pad_token``
        and ``add_bos_token``/``add_eos_token``; keyword options take precedence.
        """
        directory = Path(path)
        data = _load_json((directory / "tokenizer.json").read_bytes())
        config_path = directory / "tokenizer_config.json"
        config = _load_json(config_path.read_bytes()) if config_path.exists() else {}
        names = {}
        for key in ("bos", "eos", "pad"):
            token = _token_text(config.get(f"{key}_token"), f"{key}_token")
            if token is not None and f"{key}_token_id" not in options:
                names[f"{key}_token_id"] = token
        for key in ("add_bos_token", "add_eos_token"):
            if key in config and key not in options:
                options[key] = bool(config[key])
        flags = {key: options.pop(key) for key in ("add_bos_token", "add_eos_token")
                 if key in options}
        tokenizer = cls.from_json(data, **options)
        for key, token in names.items():
            token_id = tokenizer.token_to_id(token)
            if token_id is None:
                raise ValueError(f"tokenizer_config.json {key[:-3]} {token!r} is not a token")
            setattr(tokenizer, key, token_id)
        for key, value in flags.items():
            setattr(tokenizer, key, value)
        if tokenizer.add_bos_token and tokenizer.bos_token_id is None:
            raise ValueError("add_bos_token requires a bos_token")
        if tokenizer.add_eos_token and tokenizer.eos_token_id is None:
            raise ValueError("add_eos_token requires an eos_token")
        return tokenizer

    @classmethod
    def from_gguf_metadata(cls, metadata, *, pretokenizer=None, normalizer=None, **options):
        """Build from GGUF metadata already read into a dict (``tokenizer.ggml.*``).

        Like llama.cpp, no normalizer is applied unless ``normalizer=`` is given
        (Hugging Face Qwen tokenizers use ``"NFC"``). Token types 3 (control)
        and 4 (user defined) become special and plain added tokens; type 5
        (unused, e.g. ``[PAD...]``) decodes to nothing; others are vocabulary.
        """
        if not isinstance(metadata, Mapping):
            raise TypeError("metadata must be a mapping")
        model = metadata.get("tokenizer.ggml.model")
        if model != "gpt2":
            raise ValueError(
                f"unsupported GGUF tokenizer model {model!r}; only 'gpt2' (byte-level BPE)"
            )
        tokens = metadata.get("tokenizer.ggml.tokens")
        if not isinstance(tokens, (list, tuple)) or not tokens:
            raise ValueError("GGUF metadata needs a non-empty tokenizer.ggml.tokens list")
        types = metadata.get("tokenizer.ggml.token_type")
        if types is not None and (
            not isinstance(types, (list, tuple)) or len(types) != len(tokens)
            or not all(type(value) is int for value in types)
        ):
            raise ValueError("tokenizer.ggml.token_type must be a list of ints, one per token")
        if pretokenizer is None:
            pre = metadata.get("tokenizer.ggml.pre")
            pretokenizer = _GGUF_PRE_NAMES.get(pre)
            if pretokenizer is None:
                raise ValueError(
                    f"unknown GGUF pre-tokenizer {pre!r}; known: {sorted(_GGUF_PRE_NAMES)}. "
                    "Pass pretokenizer= to override"
                )
        vocab = {}
        added = []
        unused = []
        for token_id, token in enumerate(tokens):
            if not isinstance(token, str):
                raise ValueError("tokenizer.ggml.tokens must contain strings")
            token_type = types[token_id] if types is not None else 1
            if token_type in (3, 4):
                added.append({"id": token_id, "content": token, "special": token_type == 3,
                              "normalized": False})
            elif token in vocab:
                raise ValueError(f"duplicate GGUF token {token!r}")
            else:
                vocab[token] = token_id
                if token_type == 5:
                    unused.append(token_id)
        for key, name in (("bos_token_id", "bos"), ("eos_token_id", "eos"),
                          ("pad_token_id", "padding")):
            options.setdefault(key, metadata.get(f"tokenizer.ggml.{name}_token_id"))
        for key in ("add_bos_token", "add_eos_token"):
            options.setdefault(key, bool(metadata.get(f"tokenizer.ggml.{key}", False)))
        options.setdefault("unused_token_ids", unused)
        merges = metadata.get("tokenizer.ggml.merges")
        return cls(vocab, [] if merges is None else merges, added_tokens=added,
                   pretokenizer=pretokenizer, normalizer=normalizer, **options)

    def to_json(self):
        """A Hugging Face ``tokenizer.json`` object for this tokenizer (a dict).

        ``from_json(to_json())`` encodes and decodes like this tokenizer.
        BOS/EOS/PAD ids live in ``tokenizer_config.json`` in Hugging Face
        models, so they are not part of it. Unused ids (GGUF type 5) stay in
        the vocabulary and are listed in a ``wsbuilder`` extension key. A
        custom pre-tokenizer function cannot be written and raises
        ``ValueError``.
        """
        name = self._pretokenizer_name
        byte_level = {"type": "ByteLevel", "add_prefix_space": self.add_prefix_space,
                      "trim_offsets": False, "use_regex": name == "gpt2"}
        if name in ("qwen2", "llama3"):
            pattern = QWEN2_PATTERN if name == "qwen2" else LLAMA3_PATTERN
            pre_tokenizer = {"type": "Sequence", "pretokenizers": [
                {"type": "Split", "pattern": {"Regex": pattern}, "behavior": "Isolated",
                 "invert": False},
                byte_level,
            ]}
        elif name in ("gpt2", "none"):
            pre_tokenizer = byte_level
        else:
            raise ValueError("a custom pre-tokenizer function cannot be written to tokenizer.json")
        normalizer = None
        if self._normalizer:
            steps = [{"type": form} for form in self._normalizer]
            normalizer = steps[0] if len(steps) == 1 else {"type": "Sequence",
                                                           "normalizers": steps}
        merges = sorted(self._ranks.items(), key=lambda item: item[1])
        data = {
            "version": "1.0",
            "truncation": None,
            "padding": None,
            "added_tokens": [
                {"id": t.id, "content": t.content, "single_word": t.single_word,
                 "lstrip": t.lstrip, "rstrip": t.rstrip, "normalized": t.normalized,
                 "special": t.special}
                for t in sorted(self._added.values(), key=lambda t: t.id)
            ],
            "normalizer": normalizer,
            "pre_tokenizer": pre_tokenizer,
            "post_processor": None,
            "decoder": {"type": "ByteLevel", "add_prefix_space": True, "trim_offsets": True,
                        "use_regex": True},
            "model": {
                "type": "BPE",
                "dropout": None,
                "unk_token": None,
                "continuing_subword_prefix": None,
                "end_of_word_suffix": None,
                "fuse_unk": False,
                "byte_fallback": False,
                "ignore_merges": self.ignore_merges,
                "vocab": {token: token_id for token_id, token in sorted(self._id_to_token.items())},
                "merges": [[first, second] for (first, second), _ in merges],
            },
        }
        if self._unused:
            data["wsbuilder"] = {"unused_token_ids": sorted(self._unused)}
        return data

    # -- vocabulary -------------------------------------------------------

    def _check_id(self, token_id, name):
        if token_id is None:
            return None
        if type(token_id) is not int or self.id_to_token(token_id) is None:
            raise ValueError(f"{name} {token_id!r} is not a token id")
        return token_id

    @property
    def vocab_size(self):
        return self._vocab_size

    @property
    def special_tokens(self):
        """Special added tokens, as ``{content: id}``."""
        return {t.content: t.id for t in self._added.values() if t.special}

    @property
    def added_tokens(self):
        """All added tokens (special or not), as ``{content: id}``."""
        return {t.content: t.id for t in self._added.values()}

    def token_to_id(self, token):
        """Id of a vocabulary or added token, or ``None``."""
        added = self._added_by_content.get(token)
        if added is not None:
            return added.id
        return self._vocab.get(token)

    def id_to_token(self, token_id):
        """Token string of an id (byte-level form for vocabulary tokens), or ``None``."""
        added = self._added.get(token_id)
        if added is not None:
            return added.content
        return self._id_to_token.get(token_id)

    # -- encoding ---------------------------------------------------------

    def _normalize(self, text):
        for form in self._normalizer:
            text = unicodedata.normalize(form, text)
        return text

    def _bpe(self, word):
        cached = self._cache.get(word)
        if cached is not None:
            return cached
        if self.ignore_merges and word in self._vocab:
            ids = (self._vocab[word],)
        else:
            symbols = _merge_word(word, self._ranks)
            ids = []
            for symbol in symbols:
                token_id = self._vocab.get(symbol)
                if token_id is None:
                    raise ValueError(f"byte-level symbol {symbol!r} is missing from the vocab")
                ids.append(token_id)
            ids = tuple(ids)
        if self._cache_size:
            if len(self._cache) >= self._cache_size:
                self._cache.clear()
            self._cache[word] = ids
        return ids

    def _encode_text(self, text, ids):
        if self.add_prefix_space and not text.startswith(" "):
            text = " " + text
        for word in self._pretokenize(text):
            if word:
                ids.extend(self._bpe("".join(_BYTE_ENCODER[b] for b in word.encode("utf-8"))))

    def encode(self, text, *, add_special_tokens=False, allow_special=True,
               split_added_tokens=True):
        """Token ids of ``text``.

        Added tokens are matched first (leftmost-longest). With
        ``allow_special=False`` special tokens are encoded as plain text; with
        ``split_added_tokens=False`` all added tokens are, special or not
        (e.g. ``<think>``). ``add_special_tokens=True`` adds BOS/EOS when
        ``add_bos_token``/``add_eos_token`` are set.

        The default ``allow_special=True`` matches Hugging Face, so control
        tokens written inside the text become control ids. For untrusted text
        use ``allow_special=False`` or ``split_added_tokens=False``, or
        ``encode_chat`` for chat prompts.
        """
        if not isinstance(text, str):
            raise TypeError("text must be a string")
        raw_matcher, normalized_matcher = (self._matchers[bool(allow_special)]
                                           if split_added_tokens else (None, None))
        by_content = self._added_by_content
        pieces = [text] if text else []
        if raw_matcher is not None:
            pieces = [part for piece in pieces for part in _split_added(piece, raw_matcher,
                                                                         by_content)]
        ids = [self.bos_token_id] if add_special_tokens and self.add_bos_token else []
        for piece in pieces:
            if type(piece) is int:
                ids.append(piece)
                continue
            piece = self._normalize(piece)
            parts = ([piece] if normalized_matcher is None
                     else _split_added(piece, normalized_matcher, by_content))
            for part in parts:
                if type(part) is int:
                    ids.append(part)
                elif part:
                    self._encode_text(part, ids)
        if add_special_tokens and self.add_eos_token:
            ids.append(self.eos_token_id)
        return ids

    def encode_chat(self, messages, *, add_generation_prompt=True, enable_thinking=True,
                    system=None):
        """Token ids of the Qwen3 chat prompt, safe for untrusted message text.

        Same prompt as ``format_qwen3_chat``, but ``<|im_start|>``/``<|im_end|>``
        come only from the template: message text is encoded with
        ``split_added_tokens=False``, so a user cannot inject a system or
        assistant turn, and no added token (``<think>``, ``<tool_call>``...)
        written in a message becomes its id. ``<think>``/``</think>`` of the
        template use their added token ids when the tokenizer has them.
        ``decode(encode_chat(m)) == format_qwen3_chat(m)`` always; the ids
        equal ``encode(format_qwen3_chat(m))`` when messages contain no added
        token strings.
        """
        controls = {}
        for content in (_IM_START, _IM_END):
            token = self._added_by_content.get(content)
            if token is None or not token.special:
                raise ValueError(f"encode_chat needs {content!r} as a special token")
            controls[content] = token.id
        ids = []
        pending = []

        def flush():
            if pending:
                # Template text never holds added tokens; message text must not.
                ids.extend(self.encode("".join(pending), split_added_tokens=False))
                pending.clear()

        for kind, text in _qwen3_chat_segments(messages, add_generation_prompt,
                                               enable_thinking, system):
            if kind == _CONTROL:
                flush()
                ids.append(controls[text])
            elif kind == _MARKER and text in self._added_by_content:
                flush()
                ids.append(self._added_by_content[text].id)
            else:
                pending.append(text)
        flush()
        return ids

    # -- decoding ---------------------------------------------------------

    def _token_bytes(self, token_id, skip_special_tokens):
        if type(token_id) is not int:
            raise ValueError("token ids must be integers")
        added = self._added.get(token_id)
        if added is not None:
            if skip_special_tokens and added.special:
                return b""
            return added.content.encode("utf-8")
        token = self._id_to_token.get(token_id)
        if token is None:
            raise ValueError(f"token id {token_id} is out of range or unassigned")
        if token_id in self._unused:
            return b""
        try:
            return bytes(_BYTE_DECODER[char] for char in token)
        except KeyError:
            # Same fallback as the Hugging Face ByteLevel decoder.
            return token.encode("utf-8")

    def decode(self, ids, *, skip_special_tokens=False):
        """Text of ``ids``; invalid UTF-8 becomes U+FFFD."""
        data = bytearray()
        for token_id in ids:
            data += self._token_bytes(token_id, skip_special_tokens)
        return data.decode("utf-8", errors="replace")

    def stream_decoder(self, *, skip_special_tokens=False):
        """Incremental decoder for streaming generation (``BPEStreamDecoder``)."""
        return BPEStreamDecoder(self, skip_special_tokens=skip_special_tokens)


class BPEStreamDecoder:
    """Decodes ids one at a time, holding back incomplete UTF-8 sequences."""

    def __init__(self, tokenizer, *, skip_special_tokens=False):
        if not isinstance(tokenizer, BPETokenizer):
            raise TypeError("tokenizer must be a BPETokenizer")
        self.tokenizer = tokenizer
        self.skip_special_tokens = bool(skip_special_tokens)
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")

    def push(self, token_id):
        """Text completed by ``token_id`` (may be empty)."""
        return self._decoder.decode(self.tokenizer._token_bytes(token_id,
                                                                self.skip_special_tokens))

    def flush(self):
        """Pending bytes (U+FFFD if incomplete) and reset for a new stream."""
        text = self._decoder.decode(b"", final=True)
        self._decoder.reset()
        return text


_CHAT_ROLES = ("system", "user", "assistant")


def _chat_text(value, name):
    if value is None:
        return ""
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    return value


_IM_START = "<|im_start|>"
_IM_END = "<|im_end|>"
_THINK = "<think>"
_END_THINK = "</think>"
# Segment kinds: control tokens, template think tags, template text, and
# message text (untrusted: never turned into special tokens by encode_chat).
_CONTROL, _MARKER, _TEMPLATE, _CONTENT = range(4)


def _qwen3_chat_segments(messages, add_generation_prompt, enable_thinking, system):
    if isinstance(messages, (str, bytes, Mapping)):
        raise TypeError("messages must be a sequence of mappings")
    messages = list(messages)
    items = []
    if system is not None:
        if messages and isinstance(messages[0], Mapping) and messages[0].get("role") == "system":
            raise ValueError("pass the system prompt either as system= or as the first message")
        items.append(("system", _chat_text(system, "system"), None))
    for message in messages:
        if not isinstance(message, Mapping):
            raise TypeError("each message must be a mapping with role and content")
        role = message.get("role")
        if role == "tool" or message.get("tool_calls"):
            raise ValueError("tool messages and tool calls are not supported yet")
        if role not in _CHAT_ROLES:
            raise ValueError(f"unsupported chat role {role!r}; use one of {_CHAT_ROLES}")
        reasoning = message.get("reasoning_content")
        if reasoning is not None:
            reasoning = _chat_text(reasoning, "reasoning_content")
        items.append((role, _chat_text(message.get("content"), "content"), reasoning))
    last_query = len(items) - 1
    for index in range(len(items) - 1, -1, -1):
        role, content, _ = items[index]
        if role == "user" and not (content.startswith("<tool_response>")
                                   and content.endswith("</tool_response>")):
            last_query = index
            break
    segments = []
    for index, (role, content, reasoning) in enumerate(items):
        segments += [(_CONTROL, _IM_START), (_TEMPLATE, role + "\n")]
        if role == "assistant":
            if reasoning is None:
                reasoning = ""
                if _END_THINK in content:
                    reasoning = content.split(_END_THINK)[0].rstrip("\n").split(_THINK)[-1]
                    reasoning = reasoning.lstrip("\n")
                    content = content.split(_END_THINK)[-1].lstrip("\n")
            if index > last_query and (index == len(items) - 1 or reasoning):
                segments += [(_MARKER, _THINK), (_TEMPLATE, "\n"),
                             (_CONTENT, reasoning.strip("\n")), (_TEMPLATE, "\n"),
                             (_MARKER, _END_THINK), (_TEMPLATE, "\n\n")]
                content = content.lstrip("\n")
        segments += [(_CONTENT, content), (_CONTROL, _IM_END), (_TEMPLATE, "\n")]
    if add_generation_prompt:
        segments += [(_CONTROL, _IM_START), (_TEMPLATE, "assistant\n")]
        if not enable_thinking:
            segments += [(_MARKER, _THINK), (_TEMPLATE, "\n\n"), (_MARKER, _END_THINK),
                         (_TEMPLATE, "\n\n")]
    return segments


def format_qwen3_chat(messages, *, add_generation_prompt=True, enable_thinking=True,
                      system=None):
    """Qwen3 ChatML prompt, equivalent to the official template without tools.

    Assistant turns before the last user message lose their ``<think>`` block;
    later ones keep it (or ``reasoning_content``). With
    ``enable_thinking=False`` the generation prompt carries an empty think
    block. ``system=`` adds a first system message (``ValueError`` if the
    messages already start with one). Tool messages and tool calls are not
    supported yet.

    Message text is pasted as is: ``encode(prompt)`` would turn a
    ``<|im_end|>`` written by a user into a control token. For untrusted
    input use ``BPETokenizer.encode_chat`` instead.
    """
    return "".join(text for _, text in _qwen3_chat_segments(
        messages, add_generation_prompt, enable_thinking, system))
