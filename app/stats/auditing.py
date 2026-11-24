"""What the audit trail keeps of the statistics: nothing. The visit counts are gahk.dk's own totals,
copied in by a worker and rewritten as the day goes on; nobody decides them."""

from audit import trail

from .models import DailyVisitCount, PageViewDay

trail.exempt(DailyVisitCount, "gahk.dk's unique visitors per day, a statistic copied in by a worker")
trail.exempt(PageViewDay, "gahk.dk's visits per page and day, a statistic copied in by a worker")
