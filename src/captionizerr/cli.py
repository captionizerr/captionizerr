"""Command line: `captionizerr translate` and `captionizerr probe`."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import cues as cuesmod
from . import media
from .pipeline import translate
from .pricing import cost
from .profile import Profile
from .providers.anthropic_provider import AnthropicProvider
from .writer import write_next_to

VIDEO_EXT = {".mkv", ".mp4", ".m4v", ".avi", ".mov", ".ts", ".webm"}


def make_cli_provider(a):
    if a.provider == "ollama":
        from .providers.ollama import OllamaProvider
        return OllamaProvider(a.ollama_url, a.model, num_ctx=a.num_ctx)
    if a.provider == "openai":
        import os
        from .providers.openai_compat import OpenAICompatProvider
        return OpenAICompatProvider(a.base_url, a.model, os.environ.get("CAPTIONIZERR_OPENAI_API_KEY", ""))
    return AnthropicProvider(model=a.model, effort=a.effort)


def add_provider_args(p):
    p.add_argument("--provider", default="anthropic", choices=["anthropic", "ollama", "openai"])
    p.add_argument("--model", default="claude-opus-5-5", help="model id (Ollama: e.g. gemma3:12b)")
    p.add_argument("--effort", default="low", choices=["low", "medium", "high", "xhigh", "max"])
    p.add_argument("--ollama-url", default="http://localhost:11434")
    p.add_argument("--num-ctx", type=int, default=16384)
    p.add_argument("--base-url", default="", help="OpenAI-compatible base URL")


def cmd_eval_build(a) -> int:
    from . import evals
    made = evals.build(Path(a.data), Path(a.out), a.lines, a.per_file)
    print(f"{len(made)} new case(s) in {a.out}")
    for d in made:
        print("  ", d.name)
    return 0


def cmd_eval(a) -> int:
    from . import evals
    rep = evals.run(Path(a.cases), make_cli_provider(a), log=lambda m: print(m, file=sys.stderr) if a.verbose else None)
    for r in rep["cases"]:
        print(f"{r['chrf']:6.1f}  flags {r['flags']:3d}  ${r['cost']:.3f}  {'(edited) ' if r['edited'] else ''}{r['case']}")
    print(f"overall chrF {rep['chrf']}  cost ${rep['cost']:.3f}  model {rep['model']}")
    if a.report:
        Path(a.report).write_text(json.dumps(rep, indent=2, ensure_ascii=False), encoding="utf-8")
    return 0


def cmd_backfill_sources(a) -> int:
    """Store the source copy for translations made before sources were kept (needed for
    re-timing after an upgrade, the editor's instant open, and eval cases)."""
    from .config import Settings
    from .maintenance import SKIP_DIRS
    from .reuse import store_source, stored_source
    from .writer import _SIDECAR
    import os
    s = Settings.from_env()
    if a.data:
        s.data_dir = Path(a.data)
    n = 0
    for root in s.library_roots:
        for dirpath, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if d.lower() not in SKIP_DIRS and not d.startswith(".")]
            for f in sorted(files):
                m = _SIDECAR.match(f)
                if not m or m.group("forced"):
                    continue
                out = Path(dirpath) / f
                if stored_source(s.data_dir / "sources", out):
                    continue
                video = next((Path(dirpath) / (m.group("stem") + e) for e in s.VIDEO_EXT
                              if (Path(dirpath) / (m.group("stem") + e)).is_file()), None)
                if not video:
                    continue
                src = media.find_source(video, s.cache_dir, s.source_langs, m.group("lang"), s.any_source)
                if src:
                    store_source(s.data_dir / "sources", out, src.path, src.lang)
                    n += 1
                    print(f"stored {src.lang} ({src.origin}) for {f}", flush=True)
    print(f"{n} source copies stored")
    return 0


def cmd_probe(a) -> int:
    streams, dur = media.probe(Path(a.video))
    print(f"duration: {dur/60:.1f} min" if dur else "duration: unknown")
    for s in streams:
        print(f"  #{s.index} {s.codec:8} lang={s.language} title={s.title!r} forced={s.forced} default={s.default}")
    pick = media.pick_source(streams, a.source_lang)
    print(f"source pick ({a.source_lang}): {'#%d' % pick.index if pick else 'none'}")
    return 0


def cmd_translate(a) -> int:
    src = Path(a.input)
    video = src if src.suffix.lower() in VIDEO_EXT else None
    if video:
        streams, _ = media.probe(video)
        pick = media.pick_source(streams, a.source_lang)
        if not pick:
            print(f"no {a.source_lang} text subtitle track in {video.name}", file=sys.stderr)
            return 2
        sub_path = media.extract(video, pick)
    else:
        sub_path = src
    doc = cuesmod.load(sub_path)
    profile = Profile(target_lang=a.lang, source_lang=a.source_lang, tone=a.tone, songs=a.songs,
                      max_line=a.max_line, target_cps=a.cps)
    ctx = Path(a.context).read_text(encoding="utf-8") if a.context else None
    provider = make_cli_provider(a)
    res = translate(doc, profile, provider, series_context=ctx, title=a.title, log=lambda m: print(m, file=sys.stderr))
    out_text = cuesmod.render(doc, res.translations)

    if a.out:
        out = Path(a.out)
        if out.exists():
            print(f"refusing to overwrite {out}", file=sys.stderr)
            return 3
        out.write_text(out_text, encoding="utf-8")
    elif video and a.write:
        out = write_next_to(video, a.lang, doc.fmt, out_text)
    else:
        sys.stdout.write(out_text)
        out = None

    u = res.usage
    summary = {
        "model": a.model, "effort": a.effort, "cues": len(doc.cues), "translated": sum(1 for t in res.translations.values() if t),
        "usage": vars(u), "cost_usd": round(res.cost() or 0, 4), "cost_batch_usd": round((res.cost() or 0) / 2, 4),
        "seconds": round(sum(s.seconds for s in res.steps), 1),
        "steps": [{"name": s.name, "model": s.model, "usage": vars(s.usage), "seconds": round(s.seconds, 1), "notes": s.notes} for s in res.steps],
        "issues_initial": len(res.issues_initial), "issues_final": len(res.issues_final),
        "issues_initial_kinds": {k: sum(1 for i in res.issues_initial if i.kind == k) for k in sorted({i.kind for i in res.issues_initial})},
        "issues_final_detail": [vars(i) for i in res.issues_final][:30],
        "confirmed_drops": sorted(res.confirmed_drops),
        "needs_review": res.needs_review,
        "casing": res.casing,
        "output": str(out) if out else None,
    }
    if a.report:
        Path(a.report).write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: summary[k] for k in ("model", "cues", "translated", "usage", "cost_usd", "cost_batch_usd",
                                              "seconds", "issues_initial", "issues_initial_kinds", "issues_final", "needs_review", "casing", "output")},
                     ensure_ascii=False), file=sys.stderr)
    if res.needs_review:
        print("NEEDS REVIEW: unresolved issues after the fix rounds (see the report)", file=sys.stderr)
    return 0 if not any(i.kind == "missing" for i in res.issues_final) else 1


def cmd_serve(a) -> int:
    import logging
    import uvicorn
    from .app import create_app
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    uvicorn.run(create_app(), host=a.host, port=a.port, log_level="info", access_log=False)  # URLs carry the webhook key
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="captionizerr", description="LLM subtitle translation")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("probe", help="list subtitle streams in a video")
    p.add_argument("video")
    p.add_argument("--source-lang", default="en")
    p.set_defaults(fn=cmd_probe)

    t = sub.add_parser("translate", help="translate a subtitle file or a video's subtitle track")
    t.add_argument("input", help="a .srt/.ass file or a video file")
    t.add_argument("--lang", required=True, help="target language (ISO 639-1), e.g. de")
    t.add_argument("--source-lang", default="en")
    add_provider_args(t)
    t.add_argument("--tone", default="faithful", choices=["faithful", "softened", "family"])
    t.add_argument("--songs", default="verse", choices=["verse", "literal", "original", "skip"])
    t.add_argument("--max-line", type=int, default=42)
    t.add_argument("--cps", type=float, default=20.0)
    t.add_argument("--context", help="file with series bible / glossary / character notes")
    t.add_argument("--title", help="episode title shown to the model")
    t.add_argument("--out", help="write here (never overwrites)")
    t.add_argument("--write", action="store_true", help="write <video>.captionizerr.<lang>.<fmt> next to the video")
    t.add_argument("--report", help="write a JSON report (usage, cost, issues)")
    t.set_defaults(fn=cmd_translate)

    bf = sub.add_parser("backfill-sources", help="keep source copies for translations made before 0.1")
    bf.add_argument("--data", default="", help="data folder (default: CAPTIONIZERR_DATA_DIR)")
    bf.set_defaults(fn=cmd_backfill_sources)

    eb = sub.add_parser("eval-build", help="make quality check cases from finished translations (private data)")
    eb.add_argument("--data", default="/config", help="Captionizerr data folder")
    eb.add_argument("--out", default="/config/evals")
    eb.add_argument("--lines", type=int, default=40)
    eb.add_argument("--per-file", type=int, default=2)
    eb.set_defaults(fn=cmd_eval_build)

    ev = sub.add_parser("eval", help="translate the check cases and score them against the references (chrF)")
    ev.add_argument("cases", nargs="?", default="/config/evals")
    add_provider_args(ev)
    ev.add_argument("--report", help="write the JSON result here")
    ev.add_argument("-v", "--verbose", action="store_true")
    ev.set_defaults(fn=cmd_eval)

    sv = sub.add_parser("serve", help="run the web service (webhooks, queue, status page)")
    sv.add_argument("--host", default="0.0.0.0")
    sv.add_argument("--port", type=int, default=8095)
    sv.set_defaults(fn=cmd_serve)

    a = ap.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    raise SystemExit(main())
