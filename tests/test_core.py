from pathlib import Path

import pytest

from captionizerr import cues as cuesmod
from captionizerr.pipeline import parse_response, translate
from captionizerr.pricing import Usage, cost
from captionizerr.profile import Profile
from captionizerr.providers.base import ProviderResult
from captionizerr.validate import check
from captionizerr.writer import WriteRefused, target_path, write_next_to

SRT = """1
00:00:01,000 --> 00:00:03,000
[ DOOR CREAKS ]

2
00:00:03,500 --> 00:00:06,000
ANNA: Where are you
going, Mark?

3
00:00:06,500 --> 00:00:09,000
{\\an8}<i>♪ Down the river we go ♪</i>

4
00:00:09,500 --> 00:00:10,500
Shut up.
"""


@pytest.fixture
def srt(tmp_path) -> Path:
    p = tmp_path / "ep.en.srt"
    p.write_text(SRT, encoding="utf-8")
    return p


def test_load_marks_tag_only(srt):
    doc = cuesmod.load(srt)
    assert [c.is_tag_only for c in doc.cues] == [True, False, False, False]
    assert doc.cues[1].text == "ANNA: Where are you\ngoing, Mark?"


def test_render_keeps_timing_and_drops_empty(srt):
    doc = cuesmod.load(srt)
    out = cuesmod.render(doc, {2: "Куда ты\nсобралась, Марк?", 3: "<i>♪ Вниз по реке плывём ♪</i>", 4: ""})
    assert "00:00:03,500 --> 00:00:06,000" in out
    assert "Shut" not in out and "[ DOOR" not in out
    assert "{\\an8}" in out


def test_parse_response_tolerates_noise():
    text = "Here you go:\n2|Куда ты / собралась?\n3|2.5|<i>♪ Песня ♪</i>\n4|\n99|bogus\n2|duplicate"
    got = parse_response(text, {2, 3, 4})
    assert got == {2: "Куда ты\nсобралась?", 3: "<i>♪ Песня ♪</i>", 4: ""}


def test_validate_flags(srt):
    doc = cuesmod.load(srt)
    p = Profile(target_lang="ru")
    issues = check(doc.cues, {2: "Where are you going, Mark?", 3: "x" * 60}, p)
    kinds = {(i.index, i.kind) for i in issues}
    assert (2, "leftover") in kinds
    assert (3, "too_long") in kinds
    assert (4, "missing") in kinds
    assert all(i.index != 1 for i in issues)  # tag-only cue never flagged


class FakeProvider:
    name, model = "fake", "claude-opus-5-5"

    def __init__(self):
        self.calls = []

    def complete(self, system, context, work):
        self.calls.append(work)
        if len(self.calls) == 1:   # first pass forgets cue 4
            text = "2|Куда ты / собралась, Марк?\n3|<i>♪ Вниз по реке плывём ♪</i>"
        else:
            text = "4|Заткнись."
        return ProviderResult(text, Usage(1000, 500, 0, 0), self.model, "end_turn")


def test_pipeline_retries_missing(srt):
    doc = cuesmod.load(srt)
    prov = FakeProvider()
    res = translate(doc, Profile(target_lang="ru"), prov, log=lambda *_: None)
    assert res.translations[4] == "Заткнись."
    assert "[ DOOR" not in prov.calls[0]          # tag-only cue never sent
    assert "2|2.5|ANNA: Where are you / going, Mark?" in prov.calls[0]
    assert not [i for i in res.issues_final if i.kind == "missing"]
    assert res.cost() == pytest.approx(2 * (1000 * 4 + 500 * 20) / 1e6)


def test_cost_batch_half():
    u = Usage(1_000_000, 1_000_000)
    assert cost("claude-opus-5-5", u) == 24.0
    assert cost("claude-opus-5-5", u, batch=True) == 12.0


def test_writer_guard(tmp_path):
    video = tmp_path / "Show - S01E01.mkv"
    video.write_bytes(b"\x1aE\xdf\xa3")
    out = write_next_to(video, "ru", "srt", "1\n00:00:01,000 --> 00:00:02,000\nПривет\n")
    assert out.name == "Show - S01E01.captionizerr.ru.srt"
    with pytest.raises(WriteRefused):
        write_next_to(video, "ru", "srt", "again")          # never overwrite
    with pytest.raises(WriteRefused):
        target_path(video, "../x", "srt")                   # bad language code
    with pytest.raises(WriteRefused):
        target_path(video, "ru", "mkv")                     # only srt/ass
    with pytest.raises(WriteRefused):
        write_next_to(tmp_path / "missing.mkv", "ru", "srt", "x")
    assert video.read_bytes() == b"\x1aE\xdf\xa3"            # video untouched


def test_sentence_case():
    from captionizerr.cues import sentence_case
    assert sentence_case("THEN HE'S LEVERAGE\nOVER THE FRAU, I THINK.") == "Then he's leverage\nover the frau, I think."
    assert sentence_case("-WHO? / -ME.") .startswith("-Who?")
    assert sentence_case("♪ ONE HOUR AHEAD ♪") == "♪ One hour ahead ♪"


def test_pipeline_sends_sentence_case(tmp_path):
    p = tmp_path / "caps.srt"
    p.write_text("1\n00:00:01,000 --> 00:00:03,000\nWHERE ARE YOU GOING?\n\n2\n00:00:04,000 --> 00:00:06,000\nHOME.\n", encoding="utf-8")
    doc = cuesmod.load(p)

    class P(FakeProvider):
        def complete(self, system, context, work):
            self.calls.append(work)
            return ProviderResult("1|Куда ты?\n2|Домой.", Usage(10, 10), self.model, "end_turn")
    prov = P()
    translate(doc, Profile(target_lang="ru"), prov, log=lambda *_: None)
    assert "1|2.0|Where are you going?" in prov.calls[0]
    assert doc.cues[0].text == "WHERE ARE YOU GOING?"   # source doc untouched


def _doc(tmp_path, lines):
    """lines: list of (start_sec, end_sec, text)."""
    body = "\n".join(f"{i}\n00:{s // 60:02d}:{s % 60:02d},000 --> 00:{e // 60:02d}:{e % 60:02d},000\n{t}\n"
                     for i, (s, e, t) in enumerate(lines, 1))
    p = tmp_path / "x.srt"
    p.write_text(body, encoding="utf-8")
    return cuesmod.load(p)


def _episode(tmp_path, n=30):
    lines = [(i * 3, i * 3 + 2, f"This is ordinary line number {i}") for i in range(n)]
    return _doc(tmp_path, lines)


def test_dropped_dialogue_flagged_and_confirmable(tmp_path):
    doc = _doc(tmp_path, [(1, 3, "We offer more money."), (4, 6, "All new, Wednesdays at ten on FX.")])
    p = Profile(target_lang="ru")
    issues = check(doc.cues, {1: "", 2: ""}, p)
    assert {(i.index, i.kind) for i in issues} == {(1, "dropped"), (2, "dropped")}
    assert not [i for i in check(doc.cues, {1: "", 2: ""}, p, confirmed_drops={1, 2}) if i.kind == "dropped"]


def test_speaker_label_only_not_dialogue(tmp_path):
    from captionizerr.validate import has_dialogue
    doc = _doc(tmp_path, [(1, 3, "SCHMIDT: [ GROANS ]"), (4, 6, "ANNA: Get down.")])
    assert not has_dialogue(doc.cues[0]) and has_dialogue(doc.cues[1])


def test_boundary_merge_into_neighbour(tmp_path):
    doc = _episode(tmp_path)
    tr = {c.index: "Это обычная строка номер" for c in doc.cues}
    tr[10] = "Это обычная строка номер десять и ещё вся следующая строка целиком тоже"
    tr[11] = ""
    kinds = {(i.index, i.kind) for i in check(doc.cues, tr, Profile(target_lang="ru"))}
    assert (10, "boundary") in kinds and (11, "boundary") in kinds


def test_boundary_number_moved(tmp_path):
    lines = [(i * 3, i * 3 + 2, f"Ordinary dialogue line here {chr(65 + i % 26)}") for i in range(30)]
    lines[5] = (15, 17, "Meet me at 8 tomorrow.")
    lines[6] = (18, 20, "Sure, see you then.")
    doc = _doc(tmp_path, lines)
    tr = {c.index: "Обычная строка диалога тут" for c in doc.cues}
    tr[6], tr[7] = "Встретимся завтра.", "Конечно, в 8, до встречи."
    kinds = {(i.index, i.kind) for i in check(doc.cues, tr, Profile(target_lang="ru"))}
    assert (6, "boundary") in kinds and (7, "boundary") in kinds


def test_no_boundary_false_positive_on_normal_episode(tmp_path):
    doc = _episode(tmp_path)
    tr = {c.index: "Это обычная строка номер" for c in doc.cues}
    assert not [i for i in check(doc.cues, tr, Profile(target_lang="ru")) if i.kind == "boundary"]


def test_casing_metrics():
    from captionizerr.validate import casing_metrics
    m = casing_metrics([(1, "Мы им,"), (2, "Носы отрежем."), (3, "и всё.")])
    assert m == {"lower_after_sentence_end": 1, "capital_after_comma": 1}


def test_prompt_has_boundary_and_casing_rules():
    from captionizerr.prompt import system_prompt
    s = system_prompt(Profile())
    assert "Cue boundaries are fixed" in s and "starts with a lowercase letter" in s


def test_fix_pass_confirms_promo_drop(tmp_path):
    doc = _doc(tmp_path, [(1, 3, "Where are you going?"), (4, 6, "All new, Wednesdays at ten on FX.")])

    class P(FakeProvider):
        def complete(self, system, context, work):
            self.calls.append(work)
            if len(self.calls) == 1:
                return ProviderResult("1|Куда ты идёшь?\n2|", Usage(10, 10), self.model, "end_turn")
            return ProviderResult("2|", Usage(5, 5), self.model, "end_turn")   # re-confirms the promo drop
    res = translate(doc, Profile(target_lang="ru"), P(), log=lambda *_: None)
    assert res.confirmed_drops == {2}
    assert not res.issues_final


def test_boundary_duplicate_neighbours(tmp_path):
    lines = [(i * 3, i * 3 + 2, f"Ordinary dialogue line here {chr(65 + i % 26)}") for i in range(30)]
    lines[9] = (27, 29, "So I need to -- will you tell Bud")
    lines[10] = (30, 32, "I'll have him back after lunch, okay?")
    doc = _doc(tmp_path, lines)
    tr = {c.index: "Обычная строка диалога тут" for c in doc.cues}
    tr[10] = "Скажешь Баду, что верну его после обеда?"
    tr[11] = "Передай Баду, что я верну его после обеда?"
    kinds = {(i.index, i.kind) for i in check(doc.cues, tr, Profile(target_lang="ru"))}
    assert (10, "boundary") in kinds and (11, "boundary") in kinds


def test_second_fix_round_and_needs_review(tmp_path):
    doc = _doc(tmp_path, [(1, 3, "Where are you going?"), (4, 6, "We offer more money.")])
    long_line = "Куда же ты на самом деле сейчас так торопливо направляешься, скажи мне"

    class P(FakeProvider):
        def complete(self, system, context, work):
            self.calls.append(work)
            return ProviderResult(f"1|{long_line}\n2|Мы предлагаем больше.", Usage(5, 5), self.model, "end_turn")
    prov = P()
    res = translate(doc, Profile(target_lang="ru"), prov, log=lambda *_: None, max_fix_rounds=2)
    assert len(prov.calls) == 3          # translate + 2 fix rounds, then stop
    assert res.needs_review and {i.kind for i in res.issues_final} >= {"too_long"}


def test_provider_never_marks_empty_blocks_for_cache():
    from captionizerr.providers.anthropic_provider import AnthropicProvider
    assert AnthropicProvider.content("", "work") == [{"type": "text", "text": "work"}]
    c = AnthropicProvider.content("notes", "work")
    assert c[0]["cache_control"] and "cache_control" not in c[1]


def test_short_cues_may_read_faster():
    from captionizerr.cues import Cue
    from captionizerr.profile import Profile
    from captionizerr.validate import check
    p = Profile(target_cps=20)
    short = Cue(1, 0, 1400, "Seemed real interested.")
    text = "Казалось, ему очень интересно."   # 21 cps on a 1.4 s cue: allowed (short-cue limit 32)
    assert not [i for i in check([short], {1: text}, p) if i.kind == "too_fast"]
    long_ = Cue(2, 0, 3000, "x" * 10)
    assert [i for i in check([long_], {2: "я" * 80}, p) if i.kind == "too_fast"]


def test_chrf_sanity():
    from captionizerr.evals import chrf
    assert chrf(["Привет, мир"], ["Привет, мир"]) == 100.0
    assert chrf(["совсем другое"], ["Привет, мир"]) < 20
    assert 40 < chrf(["Привет, мир!"], ["Привет мир"]) <= 100


def test_flags_leftover_labels_and_lost_italics():
    from captionizerr.cues import Cue
    from captionizerr.profile import Profile
    from captionizerr.validate import check
    p = Profile()
    c = Cue(1, 0, 3000, "Man #2:\n<i>Surely you're not telling me</i>")
    kinds = {i.kind for i in check([c], {1: "Мужчина #2:\nнеужели вы хотите сказать мне"}, p)}
    assert "label_left" in kinds
    c2 = Cue(2, 0, 3000, "<i>From Congress.</i>")
    assert "italics_lost" in {i.kind for i in check([c2], {2: "от Конгресса."}, p)}
    assert not check([c2], {2: "<i>от Конгресса.</i>"}, p)
    c3 = Cue(3, 0, 3000, "Meet me at 10:30.")
    assert not check([c3], {3: "Встретимся в 10:30."}, p)


def test_onscreen_caption_is_not_a_speaker_label():
    from captionizerr.cues import Cue
    from captionizerr.profile import Profile
    from captionizerr.validate import check, is_onscreen_caption
    p = Profile()
    title = Cue(1, 0, 1400, "TEST SUBJECT: AI COLEMAN")
    assert is_onscreen_caption(title.text)
    assert not [i for i in check([title], {1: "ПОДОПЫТНЫЙ: АЙ КОУЛМАН"}, p) if i.kind == "label_left"]
    label = Cue(2, 0, 3000, "Man #2:\n<i>Surely you're not telling me</i>")
    assert not is_onscreen_caption(label.text)
    assert [i for i in check([label], {2: "Мужчина #2:\n<i>неужели вы хотите сказать</i>"}, p) if i.kind == "label_left"]


def test_style_issues_dont_force_review():
    from captionizerr.pipeline import Result
    from captionizerr.validate import Issue
    assert not Result({}, [], [Issue(1, "label_left", "")], []).needs_review
    assert Result({}, [], [Issue(1, "too_long", "")], []).needs_review


def test_editor_flags_match_the_validator():
    from captionizerr.editor import Row, flags_for
    from captionizerr.profile import Profile
    p = Profile(max_line=42)
    rows = [Row(1, 0, 4000, "A fairly long English line here.", "Ы" * 43),     # 43 chars: within tolerance
            Row(2, 0, 4000, "Another fairly long English line.", "Ы" * 47),    # 47 chars: too long
            Row(3, 0, 1400, "TEST SUBJECT: AI COLEMAN", "ПОДОПЫТНЫЙ: АЙ КОУЛМАН")]
    f = flags_for(rows, p)
    assert f[1] == [] and f[2] == ["47 chars"] and f[3] == []
