import numpy as np

from pokescan.trigger import Status, Trigger, Verdict

CARD = np.float32([[100, 100], [172, 100], [172, 200], [100, 200]])  # 72x100, card-shaped


def nudge(quad, dx):
    return quad + np.float32([dx, 0])


class Cards:
    """Fingerprints are card names; compare says same/different, or 'unsure' for '?'."""

    def __call__(self, committed, new):
        if "?" in (committed, new):
            return "unsure"
        return "same" if committed == new else "different"


def make(**kw):
    return Trigger(compare=Cards(), **kw)


def hold(trigger, start, seconds, quad=CARD, step=0.2):
    """Poll a still card; return the times `observe` asked for a look."""
    asks, t = [], start
    while t < start + seconds - 1e-9:
        if trigger.observe(t, quad):
            asks.append(t)
        t = round(t + step, 6)
    return asks


def test_asks_for_a_look_once_the_card_is_still():
    t = make()
    assert hold(t, 0.0, 0.6) == []          # not still long enough yet
    assert t.status is Status.MOVING
    assert hold(t, 0.6, 0.2) == [0.6]
    assert t.status is Status.STEADY


def test_movement_restarts_the_still_timer():
    t = make()
    hold(t, 0.0, 0.4)
    assert not t.observe(0.4, nudge(CARD, 10))  # 10% of card height in 0.2 s: too fast
    assert hold(t, 0.6, 0.4, nudge(CARD, 10)) == []
    assert hold(t, 1.0, 0.2, nudge(CARD, 10)) == [1.0]


def test_small_jitter_is_still():
    t = make()
    hold(t, 0.0, 0.4)
    assert t.observe(0.6, nudge(CARD, 4))  # 4% of height in 0.2 s: normal hand sway


def test_sharp_new_card_fires_once():
    t = make()
    hold(t, 0.0, 0.6)
    assert t.observe(0.6, CARD)
    assert t.judge(0.6, 0.75, "Pikachu") is Verdict.FIRE
    assert t.status is Status.SCANNED
    for when in hold(t, 0.8, 3.0):
        assert t.judge(when, 0.75, "Pikachu") is Verdict.SAME


def test_blurry_card_holds_then_sends_anyway():
    t = make()
    hold(t, 0.0, 0.6)
    assert t.observe(0.6, CARD)
    assert t.judge(0.6, 0.40, "Pikachu") is Verdict.HOLD
    assert t.status is Status.FOCUSING
    asks = hold(t, 0.8, 2.6)
    assert all(t.judge(when, 0.40, "Pikachu") is Verdict.HOLD for when in asks if when < 3.6)
    late = hold(t, 3.6, 0.4)
    assert late and t.judge(late[0], 0.40, "Pikachu") is Verdict.FIRE


def test_card_that_sharpens_fires_immediately():
    t = make()
    hold(t, 0.0, 0.6)
    t.observe(0.6, CARD)
    assert t.judge(0.6, 0.40, "Pikachu") is Verdict.HOLD
    asks = hold(t, 0.8, 0.6)
    assert t.judge(asks[0], 0.76, "Pikachu") is Verdict.FIRE


def test_swap_without_lowering_needs_two_different_verdicts():
    t = make()
    hold(t, 0.0, 0.6)
    t.observe(0.6, CARD)
    t.judge(0.6, 0.75, "Pikachu")
    asks = hold(t, 1.8, 2.0)
    assert t.judge(asks[0], 0.75, "Mew") is Verdict.WAIT
    assert t.judge(asks[1], 0.75, "Mew") is Verdict.FIRE


def test_one_odd_verdict_does_not_refire():
    t = make()
    hold(t, 0.0, 0.6)
    t.observe(0.6, CARD)
    t.judge(0.6, 0.75, "Pikachu")
    asks = hold(t, 1.8, 3.0)
    verdicts = [t.judge(w, 0.75, c) for w, c in zip(asks, ["Mew", "Pikachu", "?", "Mew", "Pikachu"])]
    assert Verdict.FIRE not in verdicts


def test_unsure_verdicts_never_fire():
    t = make()
    hold(t, 0.0, 0.6)
    t.observe(0.6, CARD)
    t.judge(0.6, 0.75, "Pikachu")
    assert all(t.judge(w, 0.75, "?") is Verdict.WAIT for w in hold(t, 1.8, 4.0))


def test_card_shown_again_after_leaving_is_scanned_again():
    t = make()
    hold(t, 0.0, 0.6)
    t.observe(0.6, CARD)
    t.judge(0.6, 0.75, "Pikachu")
    for when in (1.0, 1.5, 2.1):
        t.observe(when, None)
    assert not t.present and t.status is Status.NO_CARD
    asks = hold(t, 3.0, 1.0)
    assert asks and t.judge(asks[0], 0.75, "Pikachu") is Verdict.FIRE


def test_brief_dropout_keeps_the_presentation():
    t = make()
    hold(t, 0.0, 0.6)
    t.observe(0.6, CARD)
    t.judge(0.6, 0.75, "Pikachu")
    t.observe(0.8, None)  # detector missed one poll
    asks = hold(t, 1.0, 1.0)
    assert asks and t.judge(asks[0], 0.75, "Pikachu") is Verdict.SAME


def test_cooldown_spaces_out_scans():
    t = make(cooldown=1.0, confirm_changes=1)
    hold(t, 0.0, 0.6)
    t.observe(0.6, CARD)
    t.judge(0.6, 0.75, "Pikachu")
    assert all(when >= 1.6 for when in hold(t, 0.8, 1.6))


def test_no_new_look_until_check_every_has_passed():
    t = make(check_every=0.4)
    hold(t, 0.0, 0.6)
    assert t.observe(0.6, CARD)
    t.judge(0.6, 0.40, "Pikachu")  # blurry: held, so the next look matters
    assert not t.observe(0.7, CARD)
    assert not t.observe(0.9, CARD)
    assert t.observe(1.0, CARD)
