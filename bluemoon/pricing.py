"""Exact money math for rent, the Blue Moon price and the prior-purchase check.

Nothing here touches the database or the network. Money is whole cents (int) and every calculation uses
Decimal, so $34,240 a month is exactly $410,880 a year and 4x is exactly $1,643,520. Amounts that are worked
out stay separate from amounts a person chooses (an offer is rounded by the person, never here).

Three rules run through the module:
- Unknown is not zero. A missing rent gives None (and a reason), never $0. A confirmed zero stays zero.
- A ratio only exists when its denominator is above zero.
- Money in two currencies is never mixed.

Settings come from config/pricing.yaml (PricingConfig).
"""
from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from pydantic import BaseModel, Field

CURRENCY = "USD"
CENT = Decimal("0.01")
MONTHS = Decimal(12)

TOTAL_PERIODS = ("monthly", "annual", "t12", "single_month", "partial")
RATE_PERIODS = ("monthly", "annual")
QUANTITY_KINDS = ("units", "rentable_sqft")
AUTHORITATIVE = ("total", "rate")

STATE_OK = "ok"
STATE_PROJECTION = "projection"       # one month times twelve: a guess about the year, not a trailing 12 months
STATE_REVIEW = "needs_review"         # not enough or too ambiguous to turn into a yearly figure


class MoneyError(ValueError):
    """A money value that can't be used. The message is written for Seth."""


# --- settings --------------------------------------------------------------------------------------------

class PricingConfig(BaseModel):
    """`config/pricing.yaml`. Every key is optional; these defaults apply when one is missing."""
    reference_multiple: Decimal = Decimal(4)
    comparison_multiples: list[Decimal] = Field(default_factory=lambda: [Decimal(5), Decimal(6)])
    default_unit_mix: dict[str, dict[int, Decimal]] = Field(default_factory=dict)
    nominal_sale_price: Decimal = Decimal(1000)
    earliest_year_built: int = 1700
    follow_up_days: int = Field(default=7, ge=0, le=365)
    batch_max_properties: int = Field(default=100, ge=1, le=1000)
    evaluation_date: date | str | None = None

    def today(self) -> date:
        """The date ages and years owned are worked out against (today unless the YAML fixes a date)."""
        d = self.evaluation_date
        if isinstance(d, str):
            return datetime.strptime(d.strip(), "%Y-%m-%d").date()
        return d or date.today()

    @property
    def nominal_cents(self) -> int:
        return to_cents(self.nominal_sale_price)


def load_config(raw: Mapping[str, Any] | None) -> PricingConfig:
    return PricingConfig(**dict(raw or {}))


# --- cents -----------------------------------------------------------------------------------------------

def to_decimal(value: Any, what: str = "amount") -> Decimal:
    """A finite, non-negative number from a number or text like '1,070' or '$34,240.50'."""
    if isinstance(value, bool) or value is None:
        raise MoneyError(f"The {what} is missing.")
    if isinstance(value, float) and not math.isfinite(value):
        raise MoneyError(f"The {what} is not a number.")
    text = str(value).strip().replace(",", "").replace("$", "")
    try:
        d = Decimal(text)
    except InvalidOperation:
        raise MoneyError(f"The {what} is not a number.") from None
    if not d.is_finite():
        raise MoneyError(f"The {what} is not a number.")
    if d < 0:
        raise MoneyError(f"The {what} can't be negative.")
    return d


def round_cents(dollars: Decimal) -> int:
    return int((dollars * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def to_cents(value: Any, what: str = "amount") -> int:
    """Dollars (number or text) to whole cents, rounding half up."""
    return round_cents(to_decimal(value, what))


@dataclass(frozen=True)
class Money:
    """Whole cents in one currency. Adding two currencies raises."""
    cents: int
    currency: str = CURRENCY

    def __add__(self, other: Money) -> Money:
        if self.currency != other.currency:
            raise MoneyError(f"Can't add {self.currency} and {other.currency}.")
        return Money(self.cents + other.cents, self.currency)

    def times(self, factor: Decimal | int) -> Money:
        return Money(int((Decimal(self.cents) * Decimal(factor)).quantize(Decimal(1), rounding=ROUND_HALF_UP)),
                     self.currency)


def total(parts: Iterable[Money], currency: str = CURRENCY) -> Money:
    out = Money(0, currency)
    for m in parts:
        out = out + m
    return out


# --- turning a rent figure into a yearly one ------------------------------------------------------------

@dataclass(frozen=True)
class RentInput:
    """What a person knows about the rent, exactly as they know it.

    total + total_period: a total for the whole property (monthly, annual, t12 = the last 12 months already
        added up, single_month = one month of actual rent, partial = some other stretch of months).
    rate + rate_period + quantity + quantity_kind: a rate times a count (per unit, or per rentable sq ft).
        For sq ft the area must be net rentable (area_type 'rentable'); a rate with no period is never guessed.
    authoritative: when both are entered, which one to trust ('total' | 'rate'); the other is shown as the gap.
    """
    total: Decimal | None = None
    total_period: str | None = None
    rate: Decimal | None = None
    rate_period: str | None = None
    quantity: Decimal | None = None
    quantity_kind: str | None = None
    area_type: str | None = None
    authoritative: str | None = None
    currency: str = CURRENCY


@dataclass(frozen=True)
class NormalizedRent:
    state: str                         # ok | projection | needs_review
    annual_cents: int | None
    reason: str | None = None          # why it needs review, or a note
    formula: str | None = None         # how the yearly figure was reached, in words
    used: str | None = None            # total | rate
    discrepancy_cents: int | None = None   # yearly gap between the total and rate x quantity (when both given)

    @property
    def monthly_cents(self) -> int | None:
        return None if self.annual_cents is None else round_cents(Decimal(self.annual_cents) / 100 / MONTHS)


def _review(reason: str) -> NormalizedRent:
    return NormalizedRent(STATE_REVIEW, None, reason)


def usd(cents: int) -> str:
    return f"${cents / 100:,.2f}".replace(".00", "")


def _from_total(inp: RentInput) -> NormalizedRent | None:
    if inp.total is None:
        return None
    if inp.total_period not in TOTAL_PERIODS:
        return _review("Say whether the total is monthly, yearly or the last 12 months.")
    if inp.total_period == "partial":
        return _review("A total for part of a year needs review: it is not turned into a yearly figure by guessing.")
    if inp.total_period == "annual":
        cents, text, state = round_cents(inp.total), "yearly total, used once", STATE_OK
    elif inp.total_period == "t12":
        cents, text, state = round_cents(inp.total), "last 12 months (T12), already a full year, used once", STATE_OK
    elif inp.total_period == "single_month":
        cents, text, state = round_cents(inp.total * MONTHS), "one month x 12 (a projection, not a T12)", STATE_PROJECTION
    else:
        cents, text, state = round_cents(inp.total * MONTHS), "monthly total x 12", STATE_OK
    return NormalizedRent(state, cents, formula=text, used="total")


def _from_rate(inp: RentInput) -> NormalizedRent | None:
    if inp.rate is None and inp.quantity is None:
        return None
    if inp.rate is None:
        return _review("A count or an area alone says nothing about rent: add the rate.")
    if inp.rate_period not in RATE_PERIODS:
        return _review("Say whether the rate is per month or per year.")
    if inp.quantity_kind not in QUANTITY_KINDS or inp.quantity is None or inp.quantity <= 0:
        return _review("A rate needs an explicit count of units or rentable sq ft.")
    if inp.quantity_kind == "units" and inp.quantity != inp.quantity.to_integral_value():
        return _review("The number of units must be a whole number.")
    if inp.quantity_kind == "rentable_sqft" and inp.area_type != "rentable":
        return _review("A rate per sq ft needs the net rentable area, not gross building area or an unknown one.")
    per_year = MONTHS if inp.rate_period == "monthly" else Decimal(1)
    cents = round_cents(inp.rate * inp.quantity * per_year)
    what = "units" if inp.quantity_kind == "units" else "rentable sq ft"
    word = "month" if inp.rate_period == "monthly" else "year"
    text = f"{inp.rate:,} per {word} x {inp.quantity:,} {what}" + (" x 12" if inp.rate_period == "monthly" else "")
    return NormalizedRent(STATE_OK, cents, formula=text, used="rate")


def normalize(inp: RentInput) -> NormalizedRent:
    """The yearly gross rent for what is known, or a needs-review result that says why. Rental income only."""
    by_total, by_rate = _from_total(inp), _from_rate(inp)
    if by_total is None and by_rate is None:
        return _review("No rent figure entered.")
    if by_total is None or by_rate is None:
        return by_total or by_rate     # type: ignore[return-value]
    if inp.authoritative not in AUTHORITATIVE:
        return _review("Both a total and a rate x count were entered. Choose which one to trust.")
    chosen, other = (by_total, by_rate) if inp.authoritative == "total" else (by_rate, by_total)
    if chosen.annual_cents is None:
        return chosen
    gap = None if other.annual_cents is None else abs(chosen.annual_cents - other.annual_cents)
    return NormalizedRent(chosen.state, chosen.annual_cents, formula=chosen.formula, used=chosen.used,
                          discrepancy_cents=gap,
                          reason=None if not gap else f"The other figure differs by {usd(gap)} a year.")


# --- the Blue Moon price ---------------------------------------------------------------------------------

@dataclass(frozen=True)
class Scenario:
    multiple: Decimal
    cents: int
    role: str                          # reference (the starting price) | comparison (a scenario only)


def multiple_text(d: Decimal) -> str:
    """4, 5, 4.5 (no trailing zeros)."""
    return format(d.normalize(), "f")


def scenarios(annual_cents: int | None, cfg: PricingConfig) -> list[Scenario]:
    """4x (the reference), then 5x and 6x as comparisons. Empty when the yearly rent is unknown."""
    if annual_cents is None:
        return []
    out = [Scenario(cfg.reference_multiple, round_cents(Decimal(annual_cents) / 100 * cfg.reference_multiple),
                    "reference")]
    out += [Scenario(m, round_cents(Decimal(annual_cents) / 100 * m), "comparison") for m in cfg.comparison_multiples]
    return out


def implied_multiple(price_cents: int | None, annual_cents: int | None) -> Decimal | None:
    """price / yearly rent to 2 decimals (an offer of $1,700,000 on $410,880 is 4.14). None unless the rent is above zero."""
    if price_cents is None or annual_cents is None or annual_cents <= 0:
        return None
    return (Decimal(price_cents) / Decimal(annual_cents)).quantize(CENT, rounding=ROUND_HALF_UP)


def price_per_sqft_cents(price_cents: int | None, rentable_sqft: Any) -> int | None:
    """Price per rentable sq ft in cents. Only when the rentable area is confirmed and above zero."""
    try:
        area = to_decimal(rentable_sqft, "area")
    except MoneyError:
        return None
    if price_cents is None or area <= 0:
        return None
    return round_cents(Decimal(price_cents) / 100 / area)


# --- prior purchase compared with the reference price ---------------------------------------------------

BELOW, AT, ABOVE, UNAVAILABLE = "below", "at", "above", "unavailable"
PRIOR_LABELS = {BELOW: "Below half", AT: "At half", ABOVE: "Above half", UNAVAILABLE: "Unavailable"}


@dataclass(frozen=True)
class PriorComparison:
    state: str                         # below | at | above | unavailable
    reason: str | None                 # why it is unavailable
    sale_cents: int | None
    reference_cents: int | None        # B: the 4x reference (never an edited offer); the denominator
    share_pct: Decimal | None          # P / B as a percent, one decimal
    halfway_cents: int | None          # B / 2
    gap_cents: int | None              # how far below (or above) halfway

    @property
    def label(self) -> str:
        return PRIOR_LABELS[self.state]


def prior_purchase(sale_cents: int | None, reference_cents: int | None, *, nominal_cents: int,
                   bundled: bool = False) -> PriorComparison:
    """Compare the last recorded sale price P with half of the 4x reference B, by exact cents.

    Unavailable (never a favorable sign) when P is unknown, nominal, part of a bundled or portfolio sale, or
    B is missing or zero."""
    def out(reason: str) -> PriorComparison:
        return PriorComparison(UNAVAILABLE, reason, sale_cents, reference_cents, None, None, None)
    if sale_cents is None:
        return out("The last sale price is not recorded.")
    if sale_cents < nominal_cents:
        return out(f"The recorded sale ({usd(sale_cents)}) looks like a nominal transfer, not a market price.")
    if bundled:
        return out("The sale was flagged as a bundled or portfolio sale, so its price says nothing about this property.")
    if reference_cents is None or reference_cents <= 0:
        return out("There is no Blue Moon price to compare with (the rent is unknown or zero).")
    twice = sale_cents * 2
    state = BELOW if twice < reference_cents else AT if twice == reference_cents else ABOVE
    halfway = round_cents(Decimal(reference_cents) / 200)
    share = (Decimal(sale_cents) * 100 / Decimal(reference_cents)).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
    return PriorComparison(state, None, sale_cents, reference_cents, share, halfway, abs(halfway - sale_cents))
