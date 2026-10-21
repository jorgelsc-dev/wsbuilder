"""Bounded UTF-8 tokens and deterministic character features, using only stdlib."""

import hashlib
import math
import unicodedata

__all__ = ["ByteTokenizer", "HashingTextVectorizer"]


def _positive_integer(value, name):
    if type(value) is not int or value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value


class ByteTokenizer:
    """Reversible UTF-8 bytes (IDs 0..255), without text normalization.

    These are byte tokens, not a provider/model's token accounting scheme.
    Invalid UTF-8 and isolated Unicode surrogates are rejected.
    """

    vocab_size = 256

    def __init__(self, *, max_bytes=1_000_000):
        self.max_bytes = _positive_integer(max_bytes, "max_bytes")

    def encode(self, text):
        if not isinstance(text, str):
            raise TypeError("text must be a string")
        if len(text) > self.max_bytes:
            raise ValueError("text exceeds max_bytes")
        encoded = text.encode("utf-8")
        if len(encoded) > self.max_bytes:
            raise ValueError("text exceeds max_bytes")
        return list(encoded)

    def decode(self, tokens):
        encoded = bytearray()
        for token in tokens:
            if len(encoded) >= self.max_bytes:
                raise ValueError("tokens exceed max_bytes")
            if type(token) is not int or not 0 <= token < self.vocab_size:
                raise ValueError("tokens must be integer byte IDs in 0..255")
            encoded.append(token)
        return encoded.decode("utf-8")


class HashingTextVectorizer:
    """Fixed-width counts of Unicode character n-grams, with stable hashing.

    NFC normalization and optional casefolding are applied only to features.
    Hash collisions are possible; these are lexical features, not semantic
    embeddings. ``normalize=True`` scales each vector to unit Euclidean norm.
    Empty text produces zeros. Configuration must match training and inference.
    """

    def __init__(self, n_features=256, *, ngram_range=(1, 3), lowercase=True,
                 normalize=True, max_chars=100_000):
        self.n_features = _positive_integer(n_features, "n_features")
        if self.n_features > 65_536:
            raise ValueError("n_features must not exceed 65536")
        if not isinstance(ngram_range, (tuple, list)) or len(ngram_range) != 2:
            raise ValueError("ngram_range must contain two integers")
        low, high = ngram_range
        if type(low) is not int or type(high) is not int or not 1 <= low <= high <= 8:
            raise ValueError("ngram_range must satisfy 1 <= low <= high <= 8")
        if type(lowercase) is not bool or type(normalize) is not bool:
            raise TypeError("lowercase and normalize must be booleans")
        self.ngram_range = (low, high)
        self.lowercase = lowercase
        self.normalize = normalize
        self.max_chars = _positive_integer(max_chars, "max_chars")

    def transform(self, text):
        if not isinstance(text, str):
            raise TypeError("text must be a string")
        if len(text) > self.max_chars:
            raise ValueError("text exceeds max_chars")
        text = unicodedata.normalize("NFC", text.casefold() if self.lowercase else text)
        if len(text) > self.max_chars:
            raise ValueError("normalized text exceeds max_chars")
        vector = [0.0] * self.n_features
        for width in range(self.ngram_range[0], self.ngram_range[1] + 1):
            for offset in range(len(text) - width + 1):
                digest = hashlib.blake2b(text[offset:offset + width].encode("utf-8"),
                                         digest_size=8).digest()
                index = int.from_bytes(digest, "big") % self.n_features
                vector[index] += 1.0
        if self.normalize:
            norm = math.sqrt(sum(value * value for value in vector))
            if norm:
                vector = [value / norm for value in vector]
        return vector

    def transform_batch(self, texts):
        """Transform a batch; the caller controls the number of documents."""
        if isinstance(texts, str):
            raise TypeError("texts must be an iterable of strings, not a string")
        return [self.transform(text) for text in texts]
