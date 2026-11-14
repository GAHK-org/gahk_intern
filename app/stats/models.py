"""Visit statistics for gahk.dk (F-012).

gahk.dk counts its own visits — views per page and unique visitors per day, without storing IP
addresses — and intern pulls the day totals from its outbox (stats.services). intern keeps them:
the webhost holds nothing durable.

DailyVisitCount is the whole site's unique visitors per day, the chart on the statistik page. Its
history goes back to the legacy counter (`gahk_counterdato`, migrated by etl_stats); it used to be
the front page only, and from the move to gahk_web it is the whole site. PageViewDay is the per-page
breakdown behind "Mest besøgte sider".
"""

from django.db import models


class DailyVisitCount(models.Model):  # gahk_counterdato
    date = models.DateField(unique=True)
    count = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["-date"]

    def __str__(self) -> str:
        return f"{self.date}: {self.count}"


class PageViewDay(models.Model):
    """One page's visits on one day on gahk.dk, as gahk.dk totalled them.

    `path` is the page's path without slashes ("" for the front page, "faciliteter/kokken"); the row
    with path "*" is the whole site, whose `visitors` is also that day's DailyVisitCount.
    """

    date = models.DateField()
    path = models.CharField(max_length=200)
    views = models.PositiveIntegerField(default=0)
    visitors = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["-date", "path"]
        constraints = [models.UniqueConstraint(fields=["date", "path"], name="stats_pageviewday_date_path")]

    def __str__(self) -> str:
        return f"{self.date} /{self.path}: {self.views}"
