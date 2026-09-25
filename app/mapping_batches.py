"""外部地址映射批次 (external-address mapping batches).

The web UI accepts a CSV with a **business reference** column and a
**raw address** column (at most 100 data rows).  Every row is scored with the
existing matcher against the *same frozen snapshot* of the standard-address
table and the whole run is persisted as one batch, reviewable later by batch
id.

Two things set a batch apart from ordinary matches:

* **Business-reference conflicts** -- rows sharing the same business reference
  but carrying different raw addresses are flagged as a conflict; the service
  never silently picks one of them (none of the conflicting rows receives a
  target id, regardless of its own score).
* **Snapshot-locked export** -- downloading the complete mapping CSV re-checks
  the current address-table digest against the one bound to the batch.  When
  the table changed in the meantime the export is refused (409) and the user
  must build a fresh batch, so an exported mapping can never silently disagree
  with what was reviewed.

Batches are **pure scoring**: the review-consensus memory is not consulted and
nothing is written to ``match_logs`` (batch runs do not create ordinary match
log entries, nor review-queue records).

Rows that were not auto-hit (``needs_review`` / ``no_match`` / reference
conflict / invalid input) keep their original business reference, ship with an
empty target id and carry an explicit reason.
"""
from __future__ import annotations

import csv as csv_module
import io
import json
import secrets
import sqlite3
from datetime import datetime, timezone
from typing import Any

from . import db
from .config import Settings
from .matcher import match_address, result_to_dict

# A single batch covers at most this many data rows.
MAX_ROWS = 100

# Row-level statuses stored on each persisted batch row.
ROW_MATCHED = "auto_matched"
ROW_REVIEW = "needs_review"
ROW_NO_MATCH = "no_match"
ROW_CONFLICT = "reference_conflict"
ROW_INVALID = "invalid"

ROW_STATUS_LABELS = {
    ROW_MATCHED: "自动命中",
    ROW_REVIEW: "待确认",
    ROW_NO_MATCH: "无匹配",
    ROW_CONFLICT: "编号冲突",
    ROW_INVALID: "无效行",
}

# Statuses that never receive a target id in the export.
BLOCKED_STATUSES = (ROW_REVIEW, ROW_NO_MATCH, ROW_CONFLICT, ROW_INVALID)

# Accepted header aliases for the business reference column (case-insensitive,
# whitespace-trimmed).  ``id`` is included because external systems usually
# label their own key that way; it never collides with the standard table's
# ``external_id`` -- batch CSVs are parsed independently of address imports.
REF_ALIASES = {
    "business_id", "business_ref", "business_reference", "ref", "reference",
    "biz_id", "biz_ref", "biz_no", "biz_code", "customer_id", "client_id",
    "order_id", "order_no", "record_id", "source_id", "source_ref",
    "external_ref", "external_reference", "external_biz_id", "ext_id",
    "ext_ref", "ext_no", "code", "no", "编号", "业务编号", "客户编号",
    "订单编号", "外部编号", "关联编号",
}

# Address header aliases for the raw-address column.  A fully composed address
# wins; otherwise the structured parts are joined exactly like address imports.
ADDRESS_ALIASES = {
    "address", "full_address", "formatted_address", "addr", "raw_address",
    "original_address", "source_address", "text", "address_text",
    "full_text", "standard_address", "原始地址", "地址", "原地址",
    "客户地址", "外部地址", "详细地址",
}
LINE1_ALIASES = {"line1", "street", "address_line1", "地址行1", "街道"}


class MappingValidationError(Exception):
    """Structural CSV validation failure (whole request rejected, 400)."""


class MappingBatchNotFound(Exception):
    """The requested mapping batch does not exist."""


class MappingTableChanged(Exception):
    """The address table changed since the batch snapshot was taken."""


# -------------------------------------------------------------- CSV parsing --


def parse_mapping_csv(content: bytes) -> tuple[list[str], list[tuple[int, dict[str, str]]]]:
    """Decode a batch CSV and return ``(headers, [(line_no, row), ...])``."""
    text = content.decode("utf-8-sig")
    reader = csv_module.reader(io.StringIO(text))
    try:
        header = next(reader)
    except StopIteration:
        raise MappingValidationError("CSV 文件为空：缺少表头")
    headers = [h.strip() for h in header]
    rows: list[tuple[int, dict[str, str]]] = []
    for raw in reader:
        # csv.reader yields [] only for a completely empty physical line.
        if not raw:
            continue
        values = list(raw)
        if len(values) < len(headers):
            values += [""] * (len(headers) - len(values))
        rows.append((reader.line_num,
                     {headers[i]: values[i] for i in range(len(headers))}))
    return headers, rows


def _pick_ref_and_address(headers: list[str]) -> tuple[str | None, str | None,
                                                        str | None]:
    """Find the business-reference column and the full-address column.

    Returns ``(ref_header, full_address_header, line1_header)``; ``None``
    members mean the column is absent.  Matching is case-insensitive on the
    trimmed header text.
    """
    ref_header = None
    full_header = None
    line1_header = None
    for header in headers:
        key = header.strip().lower()
        if ref_header is None and key in REF_ALIASES:
            ref_header = header
        # A composed-address column wins over a street column even when both
        # headers are present, so "1010 Lake Dr, Austin TX" stays one string.
        if full_header is None and key in ADDRESS_ALIASES:
            full_header = header
            continue
        if full_header is None and line1_header is None and key in LINE1_ALIASES:
            line1_header = header
    return ref_header, full_header, line1_header


def extract_rows(content: bytes) -> list[dict[str, Any]]:
    """Parse and structurally validate a batch upload into raw row records.

    Structural problems (missing headers, more than 100 data rows) raise
    :class:`MappingValidationError` -- nothing is run or persisted.  Per-row
    emptiness is soft: the row is kept and marked ``invalid`` with a reason so
    the batch report and the export still carry it.
    """
    headers, numbered = parse_mapping_csv(content)
    ref_header, full_header, line1_header = _pick_ref_and_address(headers)
    if ref_header is None:
        raise MappingValidationError(
            "CSV 缺少业务编号列：请添加如 business_id / ref / 编号 的表头")
    if full_header is None and line1_header is None:
        raise MappingValidationError(
            "CSV 缺少原始地址列：请添加如 address / raw_address / 地址 的表头"
            "（或提供 street 等结构化地址列）")
    # Structured parts other than line1 reuse the standard importer aliases
    # (city/state/postal_code/country) so a file without a full-address column
    # can still be composed the same way as address-table imports.
    part_headers = ["city", "state", "postal_code", "country"]
    part_aliases = {
        "city": {"city", "town"},
        "state": {"state", "province", "region"},
        "postal_code": {"postal_code", "postcode", "zip", "zipcode"},
        "country": {"country"},
    }
    picked_parts: dict[str, str] = {}
    for part in part_headers:
        for header in headers:
            if header.strip().lower() in part_aliases[part]:
                picked_parts[part] = header
                break

    if len(numbered) == 0:
        raise MappingValidationError("CSV 没有任何数据行（至少需要 1 行）")
    if len(numbered) > MAX_ROWS:
        raise MappingValidationError(
            f"数据行最多 {MAX_ROWS} 行，当前 {len(numbered)} 行，请拆分后再上传")

    def _compose_address(row: dict[str, str]) -> str:
        if full_header is not None:
            return (row.get(full_header) or "").strip()
        parts: list[str] = []
        for part in ["line1", *part_headers]:
            header = line1_header if part == "line1" else picked_parts.get(part)
            if header and (row.get(header) or "").strip():
                parts.append(row[header].strip())
        return ", ".join(parts).strip()

    records: list[dict[str, Any]] = []
    for line_no, row in numbered:
        ref = (row.get(ref_header) or "").strip()
        address = _compose_address(row)
        invalid_reason = None
        if not ref:
            invalid_reason = "业务编号为空"
        elif not address:
            invalid_reason = "原始地址为空"
        records.append({
            "line_no": line_no,
            "business_ref": ref,
            "raw_address": address,
            "invalid_reason": invalid_reason,
        })
    return records


# ------------------------------------------------------------------ running --


def _find_conflicts(records: list[dict[str, Any]]) -> dict[str, list[int]]:
    """Business references whose non-empty rows carry >1 distinct addresses.

    Returns ``{business_ref: [conflicting line numbers]}``; only valid (not
    soft-invalid) rows participate.  Identical repeats of the same reference +
    address are not a conflict -- the reference genuinely refers to one place.
    """
    addresses: dict[str, set[str]] = {}
    lines: dict[str, list[int]] = {}
    for rec in records:
        if rec["invalid_reason"] is not None:
            continue
        ref = rec["business_ref"]
        addresses.setdefault(ref, set()).add(rec["raw_address"])
        lines.setdefault(ref, []).append(rec["line_no"])
    return {
        ref: lines[ref]
        for ref, addrs in addresses.items()
        if len(addrs) > 1
    }


def _conflict_reason(rec: dict[str, Any], conflict_lines: list[int]) -> str:
    others = [n for n in conflict_lines if n != rec["line_no"]]
    other_text = "、".join(f"第 {n} 行" for n in others) if others else ""
    suffix = f"（不同地址的行：{other_text}）" if other_text else ""
    return (
        f"业务编号“{rec['business_ref']}”在本批次中对应多个不同的原始地址"
        f"{suffix}，不得任选一行，目标 ID 留空"
    )


def run_batch(conn: sqlite3.Connection, content: bytes, filename: str,
              settings: Settings) -> dict[str, Any]:
    """Validate and score one upload inside a single snapshot transaction.

    All rows are scored against the address rows read once inside
    ``BEGIN IMMEDIATE`` and the snapshot digest recorded alongside the results
    is therefore exactly the table the scores were computed against.
    """
    records = extract_rows(content)
    conflict_map = _find_conflicts(records)

    rows_out: list[dict[str, Any]] = []
    summary = {
        "total": len(records),
        "auto_matched": 0,
        "needs_review": 0,
        "no_match": 0,
        "reference_conflict": 0,
        "invalid": 0,
        "exportable": 0,
    }

    with db.immediate_transaction(conn):
        table_rows = db.all_addresses(conn)
        if not table_rows:
            raise MappingValidationError(
                "标准地址表为空，请先上传或加载示例数据")
        for rec in records:
            line_no = rec["line_no"]
            ref = rec["business_ref"]
            address = rec["raw_address"]
            if rec["invalid_reason"] is not None:
                status = ROW_INVALID
                reason = f"无效行：{rec['invalid_reason']}，未参与匹配"
                match: dict[str, Any] | None = None
                best_id = None
            elif ref in conflict_map:
                status = ROW_CONFLICT
                reason = _conflict_reason(rec, conflict_map[ref])
                match = result_to_dict(match_address(address, table_rows, settings))
                best_id = None
            else:
                result = match_address(address, table_rows, settings)
                match = result_to_dict(result)
                status = result.status
                reason = result.reason
                best_id = result.best.address["id"] if result.best else None

            target_id = best_id if status == ROW_MATCHED else None
            if status == ROW_MATCHED:
                summary["exportable"] += 1
            summary[status] += 1
            rows_out.append({
                "line_no": line_no,
                "business_ref": ref,
                "raw_address": address,
                "status": status,
                "status_label": ROW_STATUS_LABELS[status],
                "reason": reason,
                "target_id": target_id,
                "conflict_lines": conflict_map.get(ref, [])
                if status == ROW_CONFLICT else [],
                "match": match,
            })

        snapshot_json = db.address_table_snapshot(conn)
        snapshot = json.loads(snapshot_json)
        now = datetime.now(timezone.utc)
        batch_id = f"MAP-{now.strftime('%Y%m%dT%H%M%S')}-{secrets.token_hex(4)}"
        created_at = now.isoformat(timespec="seconds")
        db.insert_mapping_batch(
            conn,
            batch_id=batch_id,
            filename=filename,
            table_snapshot=snapshot_json,
            address_count=len(table_rows),
            rows=rows_out,
            summary=summary,
            created_at=created_at,
        )

    return _serialize(batch_id, created_at, filename, snapshot,
                      len(table_rows), rows_out, summary, current=True)


# ------------------------------------------------------------- serialization --


def _snapshot_state(conn: sqlite3.Connection,
                    snapshot_json: str) -> tuple[bool, dict[str, Any]]:
    """Whether the current address table still equals the batch snapshot."""
    current = json.loads(db.address_table_snapshot(conn))
    stored = json.loads(snapshot_json)
    return current.get("digest") == stored.get("digest"), current


def _serialize(batch_id: str, created_at: str, filename: str,
               snapshot: dict[str, Any], address_count: int,
               rows: list[dict[str, Any]], summary: dict[str, Any],
               current: bool) -> dict[str, Any]:
    return {
        "id": batch_id,
        "created_at": created_at,
        "filename": filename,
        "address_count": address_count,
        "table_snapshot": snapshot,
        "snapshot_current": current,
        "summary": summary,
        "rows": rows,
    }


def serialize_batch(conn: sqlite3.Connection, row: sqlite3.Row,
                    include_rows: bool = True) -> dict[str, Any]:
    current, current_snapshot = _snapshot_state(conn, row["table_snapshot"])
    stored_snapshot = json.loads(row["table_snapshot"])
    data: dict[str, Any] = {
        "id": row["id"],
        "created_at": row["created_at"],
        "filename": row["filename"],
        "address_count": row["address_count"],
        "table_snapshot": stored_snapshot,
        "snapshot_current": current,
        "current_snapshot": current_snapshot,
        "summary": json.loads(row["summary_json"]),
    }
    if include_rows:
        data["rows"] = json.loads(row["rows_json"])
    return data


def list_batches(conn: sqlite3.Connection, limit: int = 50,
                 offset: int = 0) -> dict[str, Any]:
    rows = db.list_mapping_batches(conn, limit=limit, offset=offset)
    items = []
    for row in rows:
        item = serialize_batch(conn, row, include_rows=False)
        items.append(item)
    return {"total": db.count_mapping_batches(conn), "items": items}


def get_batch(conn: sqlite3.Connection, batch_id: str) -> dict[str, Any]:
    row = db.get_mapping_batch(conn, batch_id)
    if row is None:
        raise MappingBatchNotFound("映射批次不存在")
    return serialize_batch(conn, row)


# ------------------------------------------------------------------ export --


EXPORT_HEADER = [
    "CSV行号", "业务编号", "原始地址", "匹配状态", "目标标准地址ID",
    "目标标准地址全文", "候选数", "首选得分", "说明",
]


def _target_text(row: dict[str, Any]) -> str:
    if row["status"] != ROW_MATCHED:
        return ""
    match = row.get("match") or {}
    for cand in match.get("candidates", []):
        if cand["address"]["id"] == row["target_id"]:
            return cand["address"]["full_text"]
    return ""


def _best_score(row: dict[str, Any]) -> Any:
    match = row.get("match") or {}
    return match.get("best_score") if match else None


def export_csv(conn: sqlite3.Connection, batch_id: str) -> tuple[str, str]:
    """Build the complete mapping CSV for a batch.

    The current address-table digest is re-checked against the snapshot bound
    to the batch; a changed table raises :class:`MappingTableChanged` and the
    caller must respond 409 (re-build the batch before exporting).

    Returns ``(filename, csv_text)`` (UTF-8 with BOM so Excel opens it
    directly).
    """
    row = db.get_mapping_batch(conn, batch_id)
    if row is None:
        raise MappingBatchNotFound("映射批次不存在")
    if db.address_table_snapshot(conn) != row["table_snapshot"]:
        raise MappingTableChanged(
            "标准地址表快照已变化（地址表在上次建批次后被修改），"
            "为避免导出与当前表不一致的映射，已拒绝导出；请重新创建批次后再下载")

    out_rows: list[list[Any]] = []
    for item in json.loads(row["rows_json"]):
        match = item.get("match") or {}
        out_rows.append([
            item["line_no"],
            item["business_ref"],
            item["raw_address"],
            item["status_label"],
            item["target_id"] if item["target_id"] is not None else "",
            _target_text(item),
            len(match.get("candidates", [])),
            _best_score(item) if _best_score(item) is not None else "",
            item["reason"],
        ])

    buf = io.StringIO()
    writer = csv_module.writer(buf, lineterminator="\r\n")
    writer.writerow(EXPORT_HEADER)
    writer.writerows(out_rows)
    filename = f"mapping_{batch_id}.csv"
    # BOM so Excel detects UTF-8 correctly.
    return filename, "﻿" + buf.getvalue()
