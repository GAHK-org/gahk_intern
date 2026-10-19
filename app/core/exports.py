"""Shared CSV / Excel download helper.

Extracted from oelkaelder's `_report_response` so the ølkælder reports, the alumneliste export and the
værelsestjek AK overview all produce byte-identical downloads instead of three near-copies drifting
apart. Callers keep their own on-screen rendering; this only handles the `?format=` branches.
"""

import csv
import io

from django.http import HttpResponse


def csv_or_xlsx_response(
    fmt: str | None, title: str, headers: list[str], data: list[list[str]], filename: str
) -> HttpResponse | None:
    """A CSV or Excel download for `?format=csv|xlsx`, or None when the caller should render HTML.

    Materialises every row in memory (openpyxl holds each cell as an object), so cap the row count at
    the call site for anything unbounded — see oelkaelder.views.EXPORT_MAX_ROWS.
    """
    if fmt == "xlsx":
        from openpyxl import Workbook

        wb = Workbook()
        ws = wb.active
        ws.title = title[:31]  # Excel's sheet-name limit
        ws.append(headers)
        for row in data:
            ws.append(row)
        buf = io.BytesIO()
        wb.save(buf)
        resp = HttpResponse(
            buf.getvalue(),
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        resp["Content-Disposition"] = f'attachment; filename="{filename}.xlsx"'
        return resp
    if fmt == "csv":
        resp = HttpResponse(content_type="text/csv; charset=utf-8")
        resp["Content-Disposition"] = f'attachment; filename="{filename}.csv"'
        resp.write("﻿")  # BOM so Excel renders Danish characters
        writer = csv.writer(resp)
        writer.writerow(headers)
        writer.writerows(data)
        return resp
    return None
