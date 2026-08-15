"""Tests for the retail chatter panel.

The blend is the interesting part: StockTwits messages that carry a
self-declared Bullish/Bearish tag are *stated positions*, which is better
evidence than any classifier output, so they outweigh inferred scores — but
only in proportion to how many of each there are.

    pytest panels/test_ticker_social.py
"""

from __future__ import annotations

from datetime import datetime, timezone

from panels import ticker_social as ts

NOW = datetime(2026, 8, 15, 15, 0, tzinfo=timezone.utc)


def msg(body="", tag=None, user="u1", created="2026-08-15T10:00:00Z"):
    entities = {"sentiment": {"basic": tag}} if tag else {}
    return {"body": body, "entities": entities, "user": {"username": user},
            "created_at": created, "id": 1}


# --------------------------------------------------------------------------- #
# Tag handling
# --------------------------------------------------------------------------- #

def test_declared_tags_are_counted_not_inferred():
    messages = [msg(tag="Bullish")] * 7 + [msg(tag="Bearish")] * 3
    p = ts.compute(messages, "XYZ", now=NOW)
    assert p["bullish"] == 7 and p["bearish"] == 3
    assert p["tagged"] == 10 and p["untagged"] == 0
    assert p["bull_pct"] == 0.7
    # A pure-tag sample maps 0.7 bullish to +0.4 on the -1..+1 scale.
    assert abs(p["blended"] - 0.4) < 1e-9


def test_untagged_messages_are_scored_with_the_wsb_lexicon():
    messages = [msg(body="to the moon, loading calls, tendies incoming")] * 4
    p = ts.compute(messages, "XYZ", now=NOW)
    assert p["tagged"] == 0 and p["untagged"] == 4
    assert p["bull_pct"] is None
    assert p["untagged_mean"] > 0.4      # plain VADER would score this 0.0
    assert p["tone"] == "Bullish"


def test_blend_weights_declared_tags_above_inferred_scores():
    # Equal counts, opposite signals: the declared side must win.
    messages = ([msg(tag="Bullish")] * 10 +
                [msg(body="bagholder here, this is rekt")] * 10)
    p = ts.compute(messages, "XYZ", now=NOW)
    assert p["blended"] > 0, p["blended"]


def test_blend_respects_sample_sizes():
    # Two tags against fifty inferred bearish bodies: the inferred side, being
    # far larger, should still pull the blend negative.
    messages = ([msg(tag="Bullish")] * 2 +
                [msg(body="this is worthless, a total scam, bankruptcy incoming")] * 50)
    p = ts.compute(messages, "XYZ", now=NOW)
    assert p["blended"] < 0, p["blended"]


def test_html_entities_are_unescaped_before_tokenising():
    # StockTwits serves "Wendy&#39;s"; left encoded it produces junk terms.
    p = ts.compute([msg(body="Wendy&#39;s squeeze")] * 3, "XYZ", now=NOW)
    terms = {t["term"] for t in p["top_terms"]}
    assert not any("&" in t for t in terms), terms
    assert "squeeze" in terms


# --------------------------------------------------------------------------- #
# Velocity
# --------------------------------------------------------------------------- #

def test_velocity_is_withheld_until_history_exists():
    p = ts.compute([msg(tag="Bullish")] * 30, "XYZ", history=[], now=NOW)
    assert p["velocity_ratio"] is None
    assert p["velocity_state"] == "unknown"
    assert p["velocity_needed"] == ts.MIN_HISTORY_DAYS


def test_velocity_uses_the_median_not_the_mean():
    # One viral day of 1000 messages must not redefine "normal" for the rest.
    history = ([{"st_msgs": 10}] * 9) + [{"st_msgs": 1000}]
    p = ts.compute([msg(tag="Bullish")] * 30, "XYZ", history=history, now=NOW)
    assert p["velocity_baseline"] == 10.0        # median, not the ~109 mean
    assert p["velocity_ratio"] == 3.0
    assert p["velocity_state"] == "spike"


def test_velocity_states():
    history = [{"st_msgs": 100}] * 10
    for count, expected in [(400, "spike"), (200, "elevated"),
                            (100, "normal"), (20, "quiet")]:
        p = ts.compute([msg(tag="Bullish")] * count, "XYZ",
                       history=history, now=NOW)
        assert p["velocity_state"] == expected, (count, p["velocity_state"])


# --------------------------------------------------------------------------- #
# Degradation
# --------------------------------------------------------------------------- #

def test_empty_stream_is_not_an_error():
    # Many small caps genuinely have no chatter; that is data, not a failure.
    p = ts.compute([], "XYZ", now=NOW)
    assert p["empty"] is True and p["n"] == 0
    assert p["blended"] is None
    assert p["tone"] == "No data"
    assert p["commentary"]["headline"].startswith("No retail chatter")


def test_thin_sample_is_flagged():
    p = ts.compute([msg(tag="Bullish")] * 3, "XYZ", now=NOW)
    assert p["thin"] is True
    assert any("too thin" in w for w in p["commentary"]["warnings"])


def test_partial_paging_is_reported_without_failing():
    p = ts.compute([msg(tag="Bullish")] * 30, "XYZ", partial=True, now=NOW)
    assert p["partial"] is True
    assert any("partial sample" in w for w in p["commentary"]["warnings"])


def test_unique_users_and_per_day_counts():
    messages = [msg(tag="Bullish", user="a", created="2026-08-14T10:00:00Z"),
                msg(tag="Bullish", user="a", created="2026-08-15T10:00:00Z"),
                msg(tag="Bearish", user="b", created="2026-08-15T11:00:00Z")]
    p = ts.compute(messages, "XYZ", now=NOW)
    assert p["unique_users"] == 2
    assert p["per_day"] == [{"date": "2026-08-14", "n": 1},
                            {"date": "2026-08-15", "n": 2}]


def test_crowded_long_is_called_out():
    p = ts.compute([msg(tag="Bullish")] * 45 + [msg(tag="Bearish")] * 5,
                   "XYZ", now=NOW)
    assert p["bull_pct"] == 0.9
    joined = " ".join(p["commentary"]["sentences"])
    assert "crowded long" in joined
    # The panel must say retail skews bullish at baseline, or the reading is
    # easily over-interpreted.
    assert "baseline" in joined
