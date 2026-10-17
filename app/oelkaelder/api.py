"""Ølkælderen's public interface — the only module other domains may import (enforced by Tach)."""

from .models import Shopper

__all__ = ["balance_ore"]


def balance_ore(resident_id: int) -> int:
    """A resident's ølkælder balance in øre, summed over all their shopper accounts. Negative = owes
    the bar."""
    return sum((s.balance_ore for s in Shopper.objects.filter(resident_id=resident_id)), 0)
