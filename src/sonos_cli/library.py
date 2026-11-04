"""Allowed roots, the path guard, and stable media URLs (FR6, FR7, AD7).

Every file path the service accepts or serves goes through `Library.guard`: it is resolved with
`realpath` and must be inside a resolved allowed root (music folders, the cache directory or the
playlists directory). Names are compared after Unicode NFC normalisation.
"""

from __future__ import annotations

import hashlib
import threading
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, unquote


class PathNotAllowed(Exception):
    """The path is outside every allowed root (or can't be a path at all)."""


def nfc(text: str) -> str:
    return unicodedata.normalize("NFC", text)


def root_id(root: Path) -> str:
    """A short, stable id for a resolved root; it's the first segment of every media URL."""
    return hashlib.sha256(nfc(str(root)).encode("utf-8")).hexdigest()[:10]


def has_traversal(rel: str) -> bool:
    """True if any `..` segment appears after any amount of percent-decoding (`..%2f`, `%252e%252e%252f`)."""
    for _ in range(4):
        if any(seg == ".." for seg in rel.replace("\\", "/").split("/")):
            return True
        decoded = unquote(rel)
        if decoded == rel:
            break
        rel = decoded
    return False


@dataclass(frozen=True)
class Library:
    music_roots: tuple[Path, ...] = ()
    extra_roots: tuple[Path, ...] = ()       # cache directory, playlists directory

    @classmethod
    def create(cls, music_roots, extra_roots=()) -> "Library":
        return cls(tuple(Path(r).resolve() for r in music_roots), tuple(Path(r).resolve() for r in extra_roots))

    @property
    def allowed_roots(self) -> tuple[Path, ...]:
        return self.music_roots + self.extra_roots

    # ---- the guard ----------------------------------------------------------------------------
    def guard(self, candidate: str | Path) -> Path:
        """Return the resolved real path if it lies inside an allowed root, else raise PathNotAllowed."""
        text = str(candidate)
        if "\x00" in text:
            raise PathNotAllowed("path contains a NUL byte")
        try:
            real = Path(text).resolve()
        except (OSError, RuntimeError, ValueError) as e:      # symlink loops, over-long names
            raise PathNotAllowed(f"can't resolve {text!r}: {e}") from e
        real_nfc = Path(nfc(str(real)))
        for root in self.allowed_roots:
            if real_nfc.is_relative_to(Path(nfc(str(root)))):
                return real
        raise PathNotAllowed(f"{real} is outside the music folders")

    def music_root_of(self, real: Path) -> Path | None:
        real_nfc = Path(nfc(str(real)))
        return next((r for r in self.music_roots if real_nfc.is_relative_to(Path(nfc(str(r))))), None)

    # ---- stable URLs --------------------------------------------------------------------------
    def root_by_id(self, rid: str) -> Path | None:
        return next((r for r in self.music_roots if root_id(r) == rid), None)

    def media_path(self, base_url: str, path: str | Path, converted: bool = False) -> str:
        """`<base>/media/<root-id>/<percent-encoded relative path>` for a file under a music root. A file that is converted to FLAC
        before it is served (`converted`) gets `.flac` added to that path (`hires.wav` becomes `hires.wav.flac`; a hi-res `.flac` keeps
        its name): an S1 decides from the extension of the URL, and refuses FLAC served under a `.wav` name."""
        real = self.guard(path)
        root = self.music_root_of(real)
        if root is None:
            raise PathNotAllowed(f"{real} is not under a music folder")
        rel = nfc(str(real))[len(nfc(str(root))):].lstrip("/")
        suffix = ".flac" if converted and not rel.lower().endswith(".flac") else ""         # a hi-res .flac is converted but already named so
        return f"{base_url.rstrip('/')}/media/{root_id(root)}/{quote(rel)}{suffix}"

    def resolve_media(self, rid: str, rel: str) -> Path:
        """The guarded real path for `/media/<rid>/<rel>`. Raises KeyError for an unknown root id."""
        root = self.root_by_id(rid)
        if root is None:
            raise KeyError(rid)
        if has_traversal(rel):
            raise PathNotAllowed("path traversal")
        return self.guard(root / rel) if not rel.startswith("/") else self.guard(rel)


class LiveLibrary:
    """The one library every part of the service shares (browse, media serving, the player, playlists), whose music roots can be
    replaced while it runs (FR23: a folder added or removed from the Music view). Each call reads the current `Library` once, so a
    request sees either the old roots or the new ones, never a mix. The cache and playlists folders stay as they were."""

    def __init__(self, library: Library):
        self._lib = library
        self._lock = threading.Lock()

    def swap_music_roots(self, music_roots) -> None:
        with self._lock:
            self._lib = Library.create(music_roots, self._lib.extra_roots)

    def __getattr__(self, name: str):                      # only reached for what this class doesn't define: the Library's own API
        return getattr(self._lib, name)
