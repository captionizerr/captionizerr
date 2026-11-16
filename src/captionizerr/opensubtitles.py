"""Share a translation on OpenSubtitles.org (opt-in, one file at a time, after review).

Uploads go through the XML-RPC API, the same one OpenSubtitles' own uploader uses
(https://github.com/opensubtitles/OpenSubtitles-Uploader-PRO, src/services/api/xmlrpc.js):
    LogIn(user, password, language, useragent) -> token
    GuessMovieFromString(token, [filename])     -> IMDb id guess for the file
    TryUploadSubtitles(token, {cd1: ...})        -> alreadyindb 0/1 (duplicate check)
    UploadSubtitles(token, {baseinfo, cd1})     -> link to the new subtitle
Every upload is marked `automatictranslation=1` (AI translation), never sent automatically,
and only from the UI after the viewer confirms they reviewed it. The user agent must be one
OpenSubtitles recognises: set CAPTIONIZERR_OPENSUBTITLES_USER_AGENT if they assign you one.
"""
from __future__ import annotations

import base64
import gzip
import hashlib
import re
import struct
import xmlrpc.client
from dataclasses import dataclass
from pathlib import Path

from . import langs

ENDPOINT = "https://api.opensubtitles.org/xml-rpc"
CHUNK = 64 * 1024


class OpenSubtitlesError(RuntimeError):
    pass


def movie_hash(path: Path) -> tuple[str, int]:
    """OpenSubtitles' video hash: file size + the sum of the 64-bit little-endian words of the
    first and last 64 KiB, modulo 2^64, as 16 hex digits. Reads 128 KiB, not the whole file."""
    size = Path(path).stat().st_size
    if size < CHUNK * 2:
        raise OpenSubtitlesError("video too small to hash")
    h = size
    with open(path, "rb") as f:
        for offset in (0, size - CHUNK):
            f.seek(offset)
            buf = f.read(CHUNK)
            for (word,) in struct.iter_unpack("<Q", buf):
                h = (h + word) & 0xFFFFFFFFFFFFFFFF
    return f"{h:016x}", size


def lang3(lang: str) -> str:
    """OpenSubtitles' sublanguageid is ISO 639-2/B (rus, ger, fre, ...)."""
    code = langs.norm(lang)
    return langs.ISO3.get(code, (lang,))[0] if code else lang


class _UA(xmlrpc.client.SafeTransport):
    def __init__(self, ua: str, timeout: float):
        super().__init__()
        self.user_agent, self._timeout = ua, timeout

    def make_connection(self, host):
        conn = super().make_connection(host)
        conn.timeout = self._timeout
        return conn


def _ok(r: dict, what: str) -> dict:
    status = str((r or {}).get("status", ""))
    if not status.startswith("200"):
        raise OpenSubtitlesError(f"{what}: {status or 'no status'}")
    return r


@dataclass
class Guess:
    imdb_id: str = ""        # digits only, as OpenSubtitles wants it
    title: str = ""
    kind: str = ""           # movie | episode | tv series
    season: int | None = None
    episode: int | None = None


class Client:
    def __init__(self, user: str, password: str, user_agent: str, endpoint: str = ENDPOINT, timeout: float = 60,
                 proxy=None):
        if not user or not password:
            raise OpenSubtitlesError("OpenSubtitles username and password are not configured")
        self.user, self.password, self.ua = user, password, user_agent
        self.rpc = proxy or xmlrpc.client.ServerProxy(endpoint, transport=_UA(user_agent, timeout), allow_none=True)
        self.token = ""

    def login(self) -> str:
        r = self.rpc.LogIn(self.user, self.password, "en", self.ua)
        status = str((r or {}).get("status", ""))
        if status.startswith("401"):
            raise OpenSubtitlesError(
                "login refused (401). Uploads use the classic site's API: sign in once at "
                "https://www.opensubtitles.org/en/login with the same account (an opensubtitles.com account "
                "works there too after that first sign-in), then test again")
        if status.startswith(("414", "415")):
            raise OpenSubtitlesError(f"OpenSubtitles doesn't accept this client name ({status}); set "
                                     "CAPTIONIZERR_OPENSUBTITLES_USER_AGENT to one they assigned you")
        _ok(r, "login")
        self.token = r.get("token") or ""
        if not self.token:
            raise OpenSubtitlesError("login returned no token")
        return self.token

    def guess(self, filename: str) -> Guess:
        r = _ok(self.rpc.GuessMovieFromString(self.token, [filename]), "guess")
        data = (r.get("data") or {}).get(filename) or {}
        best = data.get("BestGuess") or {}
        g = Guess(imdb_id=str(best.get("IDMovieIMDB") or ""), title=str(best.get("MovieName") or ""),
                  kind=str(best.get("MovieKind") or ""))
        for k, attr in (("SeriesSeason", "season"), ("SeriesEpisode", "episode")):
            if str(best.get(k) or "").isdigit():
                setattr(g, attr, int(best[k]))
        return g

    def upload(self, sub: Path, video: Path, lang: str, imdb_id: str, release: str, comment: str,
               translator: str, forced: bool = False, hd: bool = False) -> dict:
        """Returns {'already': bool, 'url': str}. Raises OpenSubtitlesError on any refusal."""
        raw = Path(sub).read_bytes()
        mh, size = movie_hash(video)
        cd1 = {"subhash": hashlib.md5(raw).hexdigest(), "subfilename": Path(sub).name, "moviehash": mh,
               "moviebytesize": str(size), "moviefilename": Path(video).name}
        imdb = re.sub(r"\D", "", imdb_id or "")
        if imdb:
            cd1["idmovieimdb"] = imdb
        tried = _ok(self.rpc.TryUploadSubtitles(self.token, {"cd1": cd1}), "duplicate check")
        if str(tried.get("alreadyindb", "0")) == "1":
            return {"already": True, "url": _url(tried.get("data"))}
        if not imdb:
            raise OpenSubtitlesError("no IMDb id: OpenSubtitles needs one to file the subtitle")
        base = {"idmovieimdb": imdb, "sublanguageid": lang3(lang), "moviereleasename": release,
                "subauthorcomment": comment, "subtranslator": translator,
                "hearingimpaired": "0", "highdefinition": "1" if hd else "0",
                "foreignpartsonly": "1" if forced else "0", "automatictranslation": "1"}
        cd1["subcontent"] = base64.b64encode(gzip.compress(raw)).decode("ascii")
        r = _ok(self.rpc.UploadSubtitles(self.token, {"baseinfo": base, "cd1": cd1}), "upload")
        return {"already": False, "url": _url(r.get("data"))}


def _url(data) -> str:
    if isinstance(data, str):
        return data
    if isinstance(data, list) and data and isinstance(data[0], dict):
        data = data[0]
    if isinstance(data, dict):
        for k in ("SubDownloadLink", "ZipDownloadLink", "SubtitlesLink", "url"):
            if data.get(k):
                return str(data[k])
        if data.get("IDSubtitle"):
            return f"https://www.opensubtitles.org/subtitles/{data['IDSubtitle']}"
    return ""
