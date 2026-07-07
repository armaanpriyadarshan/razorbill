from razorbill.transcript import Utterance, _clock, dedupe_echo, merge, to_markdown


def seg(start, text, prob=0.1, speaker=None):
    s = {"start": start, "text": text, "no_speech_prob": prob}
    if speaker:
        s["speaker"] = speaker
    return s


def test_merge_interleaves_by_time():
    me = [seg(5, "Hi everyone"), seg(12, "Ship on Friday?")]
    them = [seg(8, "Hey"), seg(15, "Yes")]
    got = merge(me, them)
    assert [(u.speaker, u.text) for u in got] == [
        ("Me", "Hi everyone"),
        ("Them", "Hey"),
        ("Me", "Ship on Friday?"),
        ("Them", "Yes"),
    ]


def test_merge_drops_whisper_hallucinations():
    me = [seg(5, "Real speech"), seg(200, "Thanks for watching!", prob=0.95)]
    got = merge(me, [])
    assert len(got) == 1
    assert got[0].text == "Real speech"


def test_merge_coalesces_same_speaker():
    me = [seg(5, "First."), seg(10, "Second."), seg(100, "Later.")]
    got = merge(me, [])
    assert len(got) == 2
    assert got[0].text == "First. Second."


def test_diarized_them_gets_speaker_labels():
    them = [seg(5, "One", speaker="A"), seg(10, "Two", speaker="B")]
    got = merge([], them)
    assert [u.speaker for u in got] == ["Them (A)", "Them (B)"]


def test_single_them_speaker_stays_plain():
    them = [seg(5, "One", speaker="A"), seg(60, "Two", speaker="A")]
    assert all(u.speaker == "Them" for u in merge([], them))


def test_dedupe_echo_drops_speaker_bleed():
    them = [seg(8, "So, the quarterly numbers look good.")]
    me = [seg(9.5, "so the quarterly numbers look good"), seg(20, "I will send the report")]
    got = dedupe_echo(me, them)
    assert len(got) == 1
    assert got[0]["text"] == "I will send the report"


def test_dedupe_echo_keeps_distinct_speech_at_same_time():
    them = [seg(8, "Let me share my screen")]
    me = [seg(8.5, "The deploy failed twice yesterday")]
    assert len(dedupe_echo(me, them)) == 1


def test_clock_format():
    assert _clock(0) == "00:00:00"
    assert _clock(3671) == "01:01:11"


def test_to_markdown():
    md = to_markdown([Utterance(5, "Me", "Hello")])
    assert md == "**[00:00:05] Me:** Hello"
