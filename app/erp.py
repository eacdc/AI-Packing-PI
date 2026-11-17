"""Read-only job lookup in IndusEnterprise (Kolkata) for picking the lot's job on the touchscreen."""
from __future__ import annotations

import os

# Active jobs only. JobBookingNo comes in two shapes (J03704_26_27 and J08232/26-27),
# so the operator just types the digits.
JOB_SEARCH_SQL = """
SELECT TOP {limit}
    j.JobBookingNo AS job_no,
    j.JobName      AS job_name,
    l.LedgerName   AS client,
    j.OrderQuantity AS order_qty
FROM JobBookingJobCard j
LEFT JOIN LedgerMaster l ON l.LedgerID = j.LedgerID
WHERE j.CompanyID = 2
  AND ISNULL(j.IsCancel, 0) = 0
  AND ISNULL(j.IsClose, 0) = 0
  AND ISNULL(j.IsDeletedTransaction, 0) = 0
  AND (j.JobBookingNo LIKE %s OR j.JobName LIKE %s)
ORDER BY j.JobBookingID DESC
"""


# Every box (packing-slip label) that holds this job, with every title in that box.
# A mixed box has one row per job under one BarcodeNo (e.g. 6 titles x 12 books).
BOX_LIST_SQL = """
SELECT p.BarcodeNo AS barcode, j.JobBookingNo AS job_no, j.JobName AS job_name, p.QuantityPerBox AS qty
FROM PackingSlipBarcodeEntry p
JOIN JobBookingJobCard j ON j.JobBookingID = p.JobBookingID AND j.CompanyID = 2
WHERE p.BarcodeNo IN (
    SELECT q.BarcodeNo
    FROM PackingSlipBarcodeEntry q
    JOIN JobBookingJobCard jq ON jq.JobBookingID = q.JobBookingID AND jq.CompanyID = 2
    WHERE jq.JobBookingNo = %s)
ORDER BY p.BarcodeNo, j.JobBookingNo
"""

BOX_ITEMS_SQL = """
SELECT p.BarcodeNo AS barcode, j.JobBookingNo AS job_no, j.JobName AS job_name, p.QuantityPerBox AS qty
FROM PackingSlipBarcodeEntry p
JOIN JobBookingJobCard j ON j.JobBookingID = p.JobBookingID AND j.CompanyID = 2
WHERE p.BarcodeNo = %s
ORDER BY j.JobBookingNo
"""


class ErpClient:
    def __init__(self, cfg: dict):
        self.cfg = cfg

    def _connect(self):
        import pymssql

        user, password = os.environ.get("ERP_USER"), os.environ.get("ERP_PASSWORD")
        if not user or not password:
            raise RuntimeError("ERP_USER / ERP_PASSWORD missing in .env")
        return pymssql.connect(server=self.cfg["server"], port=str(self.cfg["port"]), user=user,
                               password=password, database=self.cfg["database"],
                               login_timeout=5, timeout=15, as_dict=True)

    def search_jobs(self, text: str, limit: int = 20) -> list[dict]:
        text = text.strip()
        if len(text) < 2:
            return []
        like = f"%{text}%"
        sql = JOB_SEARCH_SQL.replace("{limit}", str(int(limit)))
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(sql, (like, like))
            rows = cur.fetchall()
        return [{"job_no": r["job_no"], "job_name": r["job_name"], "client": (r["client"] or "").strip(),
                 "order_qty": int(r["order_qty"] or 0)} for r in rows]

    def _rows(self, sql: str, param) -> list[dict]:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(sql, (param,))
            return [{"barcode": int(r["barcode"]), "job_no": r["job_no"], "job_name": r["job_name"],
                     "qty": int(r["qty"] or 0)} for r in cur.fetchall()]

    def box_list(self, job_no: str) -> list[dict]:
        return self._rows(BOX_LIST_SQL, job_no)

    def box_items(self, barcode: int) -> list[dict]:
        return self._rows(BOX_ITEMS_SQL, int(barcode))
