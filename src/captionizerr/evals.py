"""A small, private quality check-set: does a prompt/model/provider change make translations
better or worse against references the viewer approved?

A case is a folder:  source.srt (an excerpt of the original), reference.srt (the approved
translation of exactly those lines), case.json ({"lang", "source_lang", "title"}), notes.md (optional
series notes). Cases contain copyrighted dialogue: keep them in the private data folder
(config/evals), never in a repository.

Score: chrF (character n-gram F-score, sacreBLEU's default flavour: n = 1..6, beta = 2,
whitespace ignored) against the reference, plus validator flags and cost. chrF is a coarse
signal: compare runs on the same cases, never read it as an absolute quality grade.
"""
from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path

from . import cues as cuesmod
from . import editor
from .pipeline import translate
from .profile import Profile
from .reuse import stored_source


def chrf(hyps: list[str], refs: list[str], n: int = 6, beta: float = 2.0) -> float:
    match = [0] * n
    hyp_tot = [0] * n
    ref_tot = [0] * n
    for h, r in zip(hyps, refs):
        h, r = re.sub(r"\s+", "", h), re.sub(r"\s+", "", r)
        for k in range(1, n + 1):
            hc = Counter(h[i:i + k] for i in range(len(h) - k + 1))
            rc = Counter(r[i:i + k] for i in range(len(r) - k + 1))
            match[k - 1] += sum((hc & rc).values())
            hyp_tot[k - 1] += sum(hc.values())
            ref_tot[k - 1] += sum(rc.values())
    ps = [m / t for m, t in zip(match, hyp_tot) if t]
    rs = [m / t for m, t in zip(match, ref_tot) if t]
    if not ps or not rs:
        return 0.0
    p, r = sum(ps) / len(ps), sum(rs) / len(rs)
    if p + r == 0:
        return 0.0
    return 100 * (1 + beta ** 2) * p * r / (beta ** 2 * p + r)


def _srt(rows: list[tuple[int, int, str]]) -> str:
    return "\n".join(f"{i}\n{cuesmod._ts(a)} --> {cuesmod._ts(b)}\n{t}\n" for i, (a, b, t) in enumerate(rows, 1))


def build(data_dir: Path, out: Path, lines: int = 40, per_file: int = 2) -> list[Path]:
    """Cases from finished translations that have a stored source copy. Windows are centred on
    lines the viewer edited (from the editor history) when there are any, else spread evenly."""
    data_dir, out = Path(data_dir), Path(out)
    made = []
    for meta in sorted((data_dir / "sources").glob("*.path")):
        target = Path(meta.read_text(encoding="utf-8").strip())
        st = stored_source(data_dir / "sources", target)
        if not st or not target.is_file() or target.suffix != ".srt":
            continue
        src_doc, tr_doc = cuesmod.load(st[0]), cuesmod.load(target)
        rows = [r for r in editor.build_rows(src_doc, tr_doc) if r.src and r.tr and not r.hidden]
        if len(rows) < lines:
            continue
        lang = re.search(r"\.captionizerr\.([a-zA-Z-]+)\.srt$", target.name).group(1)
        edited = _edited_rows(data_dir, target, rows)
        centres = edited[:per_file] or [len(rows) * (k + 1) // (per_file + 1) for k in range(per_file)]
        for c in centres:
            lo = max(0, min(len(rows) - lines, c - lines // 2))
            win = rows[lo:lo + lines]
            name = re.sub(r"[^\w.-]+", "_", target.stem.replace(".captionizerr", ""))[:80] + f"_{lo}"
            d = out / name
            if d.exists():
                continue
            d.mkdir(parents=True)
            (d / "source.srt").write_text(_srt([(r.start_ms, r.end_ms, r.src) for r in win]), encoding="utf-8")
            (d / "reference.srt").write_text(_srt([(r.start_ms, r.end_ms, r.tr) for r in win]), encoding="utf-8")
            (d / "case.json").write_text(json.dumps({"lang": lang, "source_lang": st[1], "title": target.stem,
                                                     "edited": bool(edited)}, ensure_ascii=False), encoding="utf-8")
            made.append(d)
    return made


def _edited_rows(data_dir: Path, target: Path, rows) -> list[int]:
    """Indices of rows whose text differs from the oldest saved version (= the machine output)."""
    from .writer import history
    versions = history(target, data_dir / "history")
    if not versions:
        return []
    first = {c.start_ms: c.text for c in cuesmod.load(versions[-1]).cues}
    return [i for i, r in enumerate(rows) if r.start_ms in first and first[r.start_ms].strip() != r.tr.strip()]


def run(cases: Path, provider, profile_for=None, log=lambda m: None) -> dict:
    """Translate every case with `provider`; returns per-case and overall chrF, flags and cost."""
    results, hyps_all, refs_all, total = [], [], [], 0.0
    for d in sorted(p for p in Path(cases).iterdir() if (p / "case.json").is_file()):
        meta = json.loads((d / "case.json").read_text(encoding="utf-8"))
        src, ref = cuesmod.load(d / "source.srt"), cuesmod.load(d / "reference.srt")
        prof = profile_for(meta["lang"], meta.get("source_lang", "en")) if profile_for else \
            Profile(target_lang=meta["lang"], source_lang=meta.get("source_lang", "en"))
        notes = (d / "notes.md").read_text(encoding="utf-8") if (d / "notes.md").is_file() else None
        res = translate(src, prof, provider, series_context=notes, title=meta.get("title"), log=log)
        hyp = [res.translations.get(c.index, "") for c in src.cues]
        refs = [c.text for c in ref.cues]
        score = chrf(hyp, refs)
        c = res.cost() or 0.0
        total += c
        hyps_all += hyp
        refs_all += refs
        results.append({"case": d.name, "chrf": round(score, 1), "flags": len(res.issues_final),
                        "cost": round(c, 4), "edited": meta.get("edited", False)})
    return {"cases": results, "chrf": round(chrf(hyps_all, refs_all), 1), "cost": round(total, 4),
            "model": getattr(provider, "model", "?")}
