"""Tests for the shared offline text scoring.

The load-bearing property is that the two analyzers stay SEPARATE objects.
`SentimentIntensityAnalyzer.lexicon.update()` mutates in place, so a single
shared instance would leak WSB slang into headline scoring — "calls" (+1.0),
"long" (+1.0) and "squeeze" (+1.5) are ordinary words in a news headline and
would silently bias every news reading.

    pytest panels/test_sentiment_util.py
"""

from __future__ import annotations

from panels import _sentiment_util as su


# --------------------------------------------------------------------------- #
# Analyzer isolation
# --------------------------------------------------------------------------- #

def test_news_and_social_analyzers_are_distinct_objects():
    assert su.NEWS is not su.SOCIAL
    assert su.NEWS.lexicon is not su.SOCIAL.lexicon


def test_all_three_analyzers_are_distinct_objects():
    analyzers = [su.NEWS, su.SOCIAL, su.MACRO]
    assert len({id(a) for a in analyzers}) == 3
    assert len({id(a.lexicon) for a in analyzers}) == 3


def test_macro_vocabulary_does_not_leak_into_the_other_analyzers():
    # "hawkish" and "stagflation" are not words a single-stock headline or a
    # WSB post uses, and neutralising "crude" would be actively wrong for a
    # stock corpus where the word keeps its ordinary English meaning.
    for word in ("hawkish", "dovish", "stagflation", "disinflation"):
        assert word in su.MACRO.lexicon
        assert word not in su.NEWS.lexicon, f"{word} leaked into NEWS"
        assert word not in su.SOCIAL.lexicon, f"{word} leaked into SOCIAL"


def test_macro_neutralises_the_subject_nouns_that_carry_a_spurious_sign():
    """These words name the subject of a macro headline, not a judgement.

    Each carries a valence in stock VADER earned in ordinary English, and on a
    macro corpus the subject appears in nearly every headline — so the sign is
    a systematic bias per panel rather than noise. "crude" at -2.7 alone makes
    every oil headline read bearish.
    """
    import vaderSentiment.vaderSentiment as vader
    plain = vader.SentimentIntensityAnalyzer().lexicon

    for word in ("crude", "credit", "treasury", "treasuries", "energy"):
        assert plain.get(word, 0) != 0, (
            f"{word!r} no longer carries a stray valence in the shipped VADER "
            f"lexicon — this neutralisation may no longer be needed")
        assert su.MACRO.lexicon[word] == 0.0
        # NEWS keeps the original value: on a stock corpus it is not wrong.
        assert su.NEWS.lexicon.get(word) == plain.get(word)


def test_macro_reads_policy_language_the_other_analyzers_miss():
    for text, direction in [("Fed officials strike a hawkish tone", -1),
                            ("Officials signal a dovish pivot", 1),
                            ("Stagflation fears mount", -1),
                            ("Oil glut deepens as output climbs", -1)]:
        assert su.score(text, su.MACRO) * direction > 0.15, text


def test_wsb_slang_does_not_leak_into_news_scoring():
    # "moon" and "tendies" are meaningless to a news reader and must stay at
    # zero there, while scoring on the social analyzer.
    for word in ("moon", "tendies", "bagholder", "rekt"):
        assert word not in su.NEWS.lexicon, f"{word} leaked into the news lexicon"
        assert word in su.SOCIAL.lexicon

    assert su.score("to the moon with tendies", su.NEWS) == 0.0
    assert su.score("to the moon with tendies", su.SOCIAL) > 0.4


def test_finance_terms_are_scored_by_both_analyzers():
    # Plain VADER reads all of these as exactly neutral.
    import vaderSentiment.vaderSentiment as vader
    plain = vader.SentimentIntensityAnalyzer()
    for text, direction in [("Q2 beats estimates, raises guidance", 1),
                            ("guidance slashed, shares plunge", -1),
                            ("announces dilutive offering", -1),
                            ("upgraded to overweight", 1)]:
        assert plain.polarity_scores(text)["compound"] * direction <= 0.35
        assert su.score(text, su.NEWS) * direction > 0.2
        assert su.score(text, su.SOCIAL) * direction > 0.2


# --------------------------------------------------------------------------- #
# Aggregation
# --------------------------------------------------------------------------- #

def test_sentiment_buckets_and_tone():
    out = su.sentiment(["shares surge on upgrade", "stock plunges on fraud probe",
                        "company files routine paperwork"], su.NEWS)
    assert out["n"] == 3
    assert out["pos"] == 1 and out["neg"] == 1
    # An even three-way split rounds to 33/33/33 = 99 unless the leftover point
    # is apportioned; all three are printed together in one sentence.
    assert out["pos_pct"] + out["neg_pct"] + out["neu_pct"] == 100


def test_percentages_always_sum_to_one_hundred():
    for counts, total in [([1, 1, 1], 3), ([1, 1, 4], 6), ([0, 0, 7], 7),
                          ([2, 3, 5], 10), ([1, 2, 0], 3), ([5, 5, 5], 15)]:
        assert sum(su._apportion(counts, total)) == 100, counts
    assert su._apportion([0, 0, 0], 0) == [0, 0, 0]


def test_sentiment_on_an_empty_corpus_degrades():
    out = su.sentiment([], su.NEWS)
    assert out == {"tone": "No data", "mean": 0.0, "pos_pct": 0, "neg_pct": 0,
                   "neu_pct": 0, "pos": 0, "neg": 0, "neu": 0, "n": 0}


def test_tone_thresholds_follow_vader_convention():
    assert su.sentiment(["shares soar on blowout beat"], su.NEWS)["tone"] == "Bullish"
    assert su.sentiment(["shares crash on bankruptcy filing"], su.NEWS)["tone"] == "Bearish"


# --------------------------------------------------------------------------- #
# Themes and stopwords
# --------------------------------------------------------------------------- #

def test_stop_for_masks_possessive_and_plain_company_forms():
    stop = su.stop_for("WEN", "Wendy's Co")
    # Headlines write both "Wendy's" and "Wendys"; leaving either in lets the
    # company name take every top theme slot.
    for form in ("wen", "$wen", "wendy's", "wendys", "wendy"):
        assert form in stop, form


def test_themes_exclude_the_subject_itself():
    titles = [
        "Wendys stock jumps on takeover bid",
        "Wendys stock rises as takeover talk builds",
        "Wendys confirms takeover approach",
    ]
    stop = su.stop_for("WEN", "Wendy's Co")
    terms = [t["term"] for t in su.themes(titles, extra_stop=stop)]
    assert not any("wendy" in t for t in terms), terms
    assert any("takeover" in t for t in terms), terms


def test_themes_on_a_tiny_corpus_returns_empty_rather_than_raising():
    assert su.themes([]) == []
    assert su.themes(["only one headline"]) == []


# --------------------------------------------------------------------------- #
# Salience and source breakdown
# --------------------------------------------------------------------------- #

def test_salient_ranks_emotive_and_on_theme_headlines_first():
    items = [
        {"title": "Company files routine paperwork", "link": "a", "source": "X"},
        {"title": "Shares crash on accounting fraud probe", "link": "b", "source": "Y"},
    ]
    ranked = su.salient(items, su.themes([i["title"] for i in items]))
    assert ranked[0]["title"].startswith("Shares crash")
    assert ranked[0]["score"] < 0


def test_by_source_splits_tone_per_publisher():
    items = [
        {"title": "stock soars on upgrade", "source": "Bull Blog"},
        {"title": "stock rallies to new high", "source": "Bull Blog"},
        {"title": "fraud probe deepens, shares plunge", "source": "Bear Wire"},
    ]
    rows = {r["source"]: r for r in su.by_source(items, su.NEWS)}
    assert rows["Bull Blog"]["n"] == 2 and rows["Bull Blog"]["mean"] > 0
    assert rows["Bear Wire"]["mean"] < 0


# --------------------------------------------------------------------------- #
# Social helpers
# --------------------------------------------------------------------------- #

def test_top_terms_strips_urls_and_cashtag_punctuation():
    texts = ["$WEN squeeze incoming https://example.test/x",
             "squeeze squeeze more squeeze"]
    terms = {t["term"]: t["count"] for t in su.top_terms(texts, {"wen"})}
    assert terms.get("squeeze") == 4
    assert "https" not in terms and "wen" not in terms


def test_cashtags_lists_co_mentions_and_excludes_the_subject():
    texts = ["$WEN and $JACK both moving", "$JACK again, plus $HTZ"]
    tags = {c["symbol"]: c["count"] for c in su.cashtags(texts, exclude="WEN")}
    assert tags == {"$JACK": 2, "$HTZ": 1}
