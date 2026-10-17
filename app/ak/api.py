"""AK's public interface — the only module other domains may import from `ak` (enforced by Tach).

Ids in, primitives out. The ledger (`AkEntry`) and the monthly schedule stay private: another domain
asks what a resident's balance is, it never sums the ledger itself.
"""

from django.db.models import Sum

from .models import AkEntry

__all__ = ["balance_krydser"]


def balance_krydser(resident_id: int) -> int:
    """A resident's AK balance in krydser. Negative = behind on labour."""
    return AkEntry.objects.filter(resident_id=resident_id).aggregate(b=Sum("delta"))["b"] or 0
