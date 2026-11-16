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
    provider = AnthropicProvider(model=a.model, effort=a.effort)
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
    t.add_argument("--lang", default="ru", help="target language (ISO 639-1)")
    t.add_argument("--source-lang", default="en")
    t.add_argument("--model", default="claude-opus-5-5")
    t.add_argument("--effort", default="low", choices=["low", "medium", "high", "xhigh", "max"])
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

    sv = sub.add_parser("serve", help="run the web service (webhooks, queue, status page)")
    sv.add_argument("--host", default="0.0.0.0")
    sv.add_argument("--port", type=int, default=8095)
    sv.set_defaults(fn=cmd_serve)

    a = ap.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    raise SystemExit(main())
