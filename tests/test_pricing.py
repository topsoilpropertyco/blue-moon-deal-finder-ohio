"""Exact money math (bluemoon/pricing.py): rent to a yearly figure, 4x / 5x / 6x, implied multiples, price per
sq ft and the prior-purchase check. Every number here is a fixture from the product spec."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from bluemoon import pricing as P
from bluemoon.pricing import RentInput, load_config, normalize

CFG = load_config({"reference_multiple": 4, "comparison_multiples": [5, 6], "nominal_sale_price": 1000})
D = Decimal


def dollars(cents: int | None) -> Decimal | None:
    return None if cents is None else Decimal(cents) / 100


def annual(**kw) -> int | None:
    return normalize(RentInput(**kw)).annual_cents


# --- yearly rent ---------------------------------------------------------------------------------------

def test_monthly_total_is_twelve_months_once():
    r = normalize(RentInput(total=D(34240), total_period="monthly"))
    assert (r.state, dollars(r.annual_cents), dollars(r.monthly_cents)) == ("ok", 410880, 34240)


def test_scenarios_four_five_six():
    s = P.scenarios(41088000, CFG)
    assert [(P.multiple_text(x.multiple), dollars(x.cents), x.role) for x in s] == [
        ("4", 1643520, "reference"), ("5", 2054400, "comparison"), ("6", 2465280, "comparison")]


def test_per_unit_monthly_with_explicit_count_matches_the_total():
    assert annual(rate=D(1070), rate_period="monthly", quantity=D(32), quantity_kind="units") == 41088000


def test_per_unit_annual_is_not_multiplied_by_twelve():
    assert annual(rate=D(12840), rate_period="annual", quantity=D(32), quantity_kind="units") == 41088000


def test_two_units_at_1000_and_three_at_1500():
    from bluemoon import napkin
    est = napkin.estimate(5, {"mix": {1: 2, 2: 3}, "rents": {1: 100000, 2: 150000}}, {}, None)
    assert (dollars(est["monthly_cents"]), dollars(est["annual_cents"])) == (6500, 78000)
    assert dollars(P.scenarios(est["annual_cents"], CFG)[0].cents) == 312000


@pytest.mark.parametrize("rate,period", [("1", "monthly"), ("12", "annual")])
def test_per_rentable_sqft_needs_area_and_period(rate, period):
    r = normalize(RentInput(rate=D(rate), rate_period=period, quantity=D(35000), quantity_kind="rentable_sqft",
                            area_type="rentable"))
    assert dollars(r.annual_cents) == 420000
    assert dollars(P.scenarios(r.annual_cents, CFG)[0].cents) == 1680000


def test_the_literal_total_is_never_scaled_to_a_guessed_unit_count():
    r = normalize(RentInput(total=D(1160), total_period="monthly"))
    assert dollars(r.annual_cents) == 13920
    assert dollars(P.scenarios(r.annual_cents, CFG)[0].cents) == 55680


def test_t12_and_annual_totals_are_used_once():
    for period in ("t12", "annual"):
        assert annual(total=D(410880), total_period=period) == 41088000


def test_single_month_is_labelled_a_projection():
    r = normalize(RentInput(total=D(34240), total_period="single_month"))
    assert (r.state, dollars(r.annual_cents)) == (P.STATE_PROJECTION, 410880)
    assert "projection" in r.formula


@pytest.mark.parametrize("inp", [
    RentInput(total=D(5000), total_period="partial"),                                    # some months: no multiplier invented
    RentInput(total=D(5000)),                                                            # no period
    RentInput(rate=D(1), quantity=D(35000), quantity_kind="rentable_sqft", area_type="rentable"),   # no rate period
    RentInput(rate=D(1), rate_period="monthly", quantity=D(35000), quantity_kind="rentable_sqft",
              area_type="gross"),                                                        # gross is not rentable
    RentInput(rate=D(1), rate_period="monthly", quantity=D(35000), quantity_kind="rentable_sqft"),  # area type unknown
    RentInput(quantity=D(35000), quantity_kind="rentable_sqft", area_type="rentable"),   # area alone infers nothing
    RentInput(rate=D(1000), rate_period="monthly"),                                      # a rate with no count
    RentInput(rate=D(1000), rate_period="monthly", quantity=D("2.5"), quantity_kind="units"),
    RentInput(),
])
def test_ambiguous_input_needs_review_and_has_no_figure(inp):
    r = normalize(inp)
    assert (r.state, r.annual_cents) == (P.STATE_REVIEW, None) and r.reason


def test_total_and_rate_together_need_a_choice_and_show_the_gap():
    both = dict(total=D(34000), total_period="monthly", rate=D(1070), rate_period="monthly", quantity=D(32),
                quantity_kind="units")
    assert normalize(RentInput(**both)).state == P.STATE_REVIEW
    by_total = normalize(RentInput(**both, authoritative="total"))
    by_rate = normalize(RentInput(**both, authoritative="rate"))
    assert (dollars(by_total.annual_cents), by_total.used, dollars(by_total.discrepancy_cents)) == (408000, "total", 2880)
    assert (dollars(by_rate.annual_cents), by_rate.used) == (410880, "rate")


def test_confirmed_zero_stays_zero_and_is_not_unknown():
    r = normalize(RentInput(total=D(0), total_period="annual"))
    assert (r.state, r.annual_cents) == (P.STATE_OK, 0)
    assert P.scenarios(0, CFG)[0].cents == 0
    assert P.scenarios(None, CFG) == []


# --- cents and currencies ------------------------------------------------------------------------------

@pytest.mark.parametrize("bad", [None, True, "", "abc", float("nan"), float("inf"), "-5", -1])
def test_bad_money_is_refused(bad):
    with pytest.raises(P.MoneyError):
        P.to_cents(bad)


def test_cents_round_half_up_and_accept_formatted_text():
    assert P.to_cents("$1,070.005") == 107001 and P.to_cents(0.1 + 0.2) == 30


def test_mixed_currencies_are_refused():
    with pytest.raises(P.MoneyError):
        P.Money(100, "USD") + P.Money(100, "CAD")
    assert (P.Money(100) + P.Money(250)).cents == 350


# --- ratios --------------------------------------------------------------------------------------------

def test_implied_multiple_and_price_per_sqft():
    assert P.implied_multiple(170000000, 41088000) == D("4.14")
    assert P.implied_multiple(164352000, 41088000) == D("4.00")
    assert P.implied_multiple(100, 0) is None and P.implied_multiple(None, 100) is None and P.implied_multiple(100, None) is None
    assert [P.price_per_sqft_cents(c, 35000) for c in (164352000, 205440000, 246528000)] == [4696, 5870, 7044]
    assert P.price_per_sqft_cents(164352000, 0) is None and P.price_per_sqft_cents(164352000, None) is None


# --- prior purchase ------------------------------------------------------------------------------------

def prior(sale, reference=110000000, **kw):
    return P.prior_purchase(None if sale is None else sale * 100, reference, nominal_cents=100000, **kw)


def test_prior_purchase_below_half():
    r = prior(527000)
    assert (r.state, r.label, r.share_pct, dollars(r.halfway_cents), dollars(r.gap_cents)) == (
        "below", "Below half", D("47.9"), 550000, 23000)


@pytest.mark.parametrize("sale,state,gap", [(550000, "at", 0), (549999, "below", 1), (550001, "above", 1)])
def test_prior_purchase_boundary_is_decided_by_exact_cents(sale, state, gap):
    r = prior(sale)
    assert (r.state, dollars(r.gap_cents)) == (state, gap)


def test_prior_purchase_by_cents_not_dollars():
    r = P.prior_purchase(54999999, 110000000, nominal_cents=100000)
    assert r.state == "below" and r.gap_cents == 1


@pytest.mark.parametrize("sale,reference,kw", [
    (None, 110000000, {}), (999, 110000000, {}), (527000, 110000000, {"bundled": True}),
    (527000, None, {}), (527000, 0, {})])
def test_prior_purchase_unavailable_is_never_favorable(sale, reference, kw):
    r = P.prior_purchase(None if sale is None else sale * 100, reference, nominal_cents=100000, **kw)
    assert (r.state, r.label) == ("unavailable", "Unavailable") and r.reason and r.share_pct is None


def test_config_defaults_and_evaluation_date():
    cfg = load_config({"evaluation_date": date(2026, 11, 5)})
    assert cfg.today() == date(2026, 11, 5) and cfg.nominal_cents == 100000 and cfg.reference_multiple == 4
    assert load_config({"evaluation_date": "2020-01-02"}).today() == date(2020, 1, 2)
    assert load_config(None).today() == date.today()
