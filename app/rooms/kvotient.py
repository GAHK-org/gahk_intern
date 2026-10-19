"""Kvotient (room-lottery) ranking math (F-004).

Months are absolute, 0-indexed (decided): month_index(year, month) = year*12 + (month-1).
K = a*100/(a+b+12) where a = months lived at GAHK (minus orlov) and b = months until study end,
both relative to the target (offer) month. Confirmed formula (incl. the +12). Higher K ranks first.
"""

from core.danish import MONTHS


def month_index(year: int, month: int) -> int:
    return year * 12 + (month - 1)


# Danish month names live in core.danish now (they were copy-pasted in three places). That list
# is 1-indexed so MONTHS[date.month] works; this module counts months from zero, hence the +1.


def month_label(index: int) -> str:
    """Human-readable 'Måned ÅÅÅÅ' for an absolute 0-indexed month index (inverse of month_index)."""
    year, month0 = divmod(index, 12)
    return f"{MONTHS[1 + month0].capitalize()} {year}"


def month_choices() -> list[tuple[int, str]]:
    """(month_number, Danish name) pairs 1..12, for <select> month pickers."""
    # MONTHS[0] is the empty placeholder that makes MONTHS[date.month] work, so skip it.
    return [(i, name.capitalize()) for i, name in enumerate(MONTHS[1:], start=1)]


def compute_k_parts(
    move_in_index: int, done_studying_index: int, target_index: int, orlov_months: int = 0
) -> dict[str, float]:
    """The K components, so callers can *show* the formula, not just the result (F-004):
    a = months lived at GAHK up to the target (minus orlov), b = months from target to study end,
    K = a·100/(a+b+12). Single source of truth for the ranking math."""
    a = (target_index - move_in_index) - orlov_months
    b = done_studying_index - target_index
    denom = a + b + 12
    k = round(a * 100 / denom, 2) if denom > 0 else 0.0
    return {"a": a, "b": b, "k": k}


def compute_k(
    move_in_index: int, done_studying_index: int, target_index: int, orlov_months: int = 0
) -> float:
    return compute_k_parts(move_in_index, done_studying_index, target_index, orlov_months)["k"]
