"""Tests for the external-address mapping batch feature."""
from __future__ import annotations

import csv
import io
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import ingest  # noqa: E402
from app.main import app, get_conn, get_settings  # noqa: E402


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def _isolated_tables(client):
    """Reset the shared test DB so other modules' data doesn't leak in."""
    conn = get_conn()
    conn.execute("DELETE FROM mapping_batches")
    conn.execute("DELETE FROM match_logs")
    conn.execute("DELETE FROM addresses")
    conn.commit()
    sample = Path(get_settings().sample_csv)
    ingest.add_many(conn, ingest.parse_csv(sample.read_bytes()))
    yield
    conn.execute("DELETE FROM mapping_batches")
    conn.commit()


def _csv(text: str, filename: str = "mapping.csv") -> dict:
    return {"file": (filename, text.encode("utf-8"), "text/csv")}


def _rows_by_line(data) -> dict[int, dict]:
    return {r["line_no"]: r for r in data["rows"]}


# ------------------------------------------------------------- happy path


def test_batch_scores_all_rows_on_same_snapshot(client):
    r = client.post("/api/mapping-batches", files=_csv(
        "business_id,address\n"
        "B1,123 main st ny\n"
        "B2,1 Milky Way Mars Base\n"
    ))
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["id"].startswith("MAP-")
    assert data["address_count"] >= 15
    assert data["table_snapshot"]["count"] == data["address_count"]
    assert data["snapshot_current"] is True
    assert data["summary"]["total"] == 2
    by_line = _rows_by_line(data)
    # line 2 (header is line 1): auto hit, target filled
    assert by_line[2]["status"] == "auto_matched"
    assert by_line[2]["business_ref"] == "B1"
    assert isinstance(by_line[2]["target_id"], int)
    assert by_line[2]["match"]["candidates"]
    # line 3: no match, target empty, reason given
    assert by_line[3]["status"] == "no_match"
    assert by_line[3]["target_id"] is None
    assert by_line[3]["reason"]


def test_needs_review_row_has_empty_target_and_candidates(client):
    r = client.post("/api/mapping-batches", files=_csv(
        "business_id,address\nB1,1010 Lake Dr Austin TX\n"
    ))
    data = r.json()
    row = data["rows"][0]
    assert row["status"] == "needs_review"
    assert row["target_id"] is None
    assert row["match"]["best_id"] is not None  # best candidate still shown
    assert row["match"]["candidates"]


def test_batch_does_not_write_match_logs(client):
    before = client.get("/api/matches", params={"limit": 1}).json()["total"]
    client.post("/api/mapping-batches", files=_csv(
        "business_id,address\n"
        "B1,123 main st ny\n"
        "B2,1 Milky Way Mars Base\n"
    ))
    after = client.get("/api/matches", params={"limit": 1}).json()["total"]
    assert after == before


def test_batch_does_not_apply_review_consensus(client):
    # A needs-review query for which a consensus exists would be auto-redirected
    # on /api/match; batches must keep the raw scoring verdict.
    q = "1010 Lake Dr, Austin TX"
    m = client.post("/api/match", json={"query": q, "save": True}).json()
    assert m["status"] == "needs_review"
    match_id = m["match_id"]
    # Claim, confirm best, then do it twice for a second record of the same key.
    claim = client.post(f"/api/matches/{match_id}/claim",
                        json={"reviewer": "tester"}).json()
    client.post(f"/api/matches/{match_id}/review", json={
        "action": "confirmed", "token": claim["token"]})
    m2 = client.post("/api/match", json={"query": q, "save": True}).json()
    match_id2 = m2["match_id"]
    claim2 = client.post(f"/api/matches/{match_id2}/claim",
                         json={"reviewer": "tester"}).json()
    client.post(f"/api/matches/{match_id2}/review", json={
        "action": "confirmed", "token": claim2["token"]})
    # Ordinary matching now applies the consensus...
    ordinary = client.post("/api/match", json={"query": q, "save": False}).json()
    assert ordinary["status"] == "auto_matched"
    assert ordinary["consensus"]["applied"] is True
    # ...but a batch run on the same query stays a raw-scoring needs_review.
    data = client.post("/api/mapping-batches", files=_csv(
        f'business_id,address\nB1,"{q}"\n')).json()
    assert data["rows"][0]["status"] == "needs_review"
    assert data["rows"][0]["target_id"] is None


# -------------------------------------------------------- reference conflicts


def test_same_ref_different_addresses_is_conflict_and_never_picked(client):
    r = client.post("/api/mapping-batches", files=_csv(
        "business_id,address\n"
        "B1,123 main st ny\n"          # would auto-hit STD-001
        "B1,1010 Lake Dr Austin TX\n"  # different address, same ref
    ))
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["summary"]["reference_conflict"] == 2
    for row in data["rows"]:
        assert row["status"] == "reference_conflict"
        assert row["target_id"] is None
        assert "编号" in row["reason"] or "多个不同" in row["reason"]
        # candidates are still recorded for review even though not picked
        assert row["match"]["candidates"]
    assert set(data["rows"][0]["conflict_lines"]) == {2, 3}


def test_identical_repeated_ref_is_not_a_conflict(client):
    r = client.post("/api/mapping-batches", files=_csv(
        "business_id,address\n"
        "B1,123 main st ny\n"
        "B1, 123 main st ny \n"  # whitespace-only difference strips to the same text
    ))
    # The stripped address strings are identical -> both auto-match.
    data = r.json()
    assert data["summary"]["reference_conflict"] == 0
    assert all(row["status"] == "auto_matched" for row in data["rows"])


def test_conflict_row_that_would_score_high_still_has_no_target(client):
    r = client.post("/api/mapping-batches", files=_csv(
        'business_id,address\n'
        'B1,"123 Main Street, New York, NY 10001"\n'  # exact hit, score 1.0
        'B1,"742 Evergreen Terrace, Springfield"\n'   # also an exact hit
    ))
    data = r.json()
    assert data["summary"]["auto_matched"] == 0
    assert data["summary"]["reference_conflict"] == 2
    assert all(row["target_id"] is None for row in data["rows"])
    # Even though both rows score at auto-hit level on their own, their stored
    # candidates prove the matcher would have picked something.
    for row in data["rows"]:
        assert row["match"]["best_score"] is not None
        assert row["match"]["best_score"] >= 0.8


# ----------------------------------------------------------------- invalid


def test_empty_ref_or_address_rows_marked_invalid(client):
    data = client.post("/api/mapping-batches", files=_csv(
        "business_id,address\n"
        ",123 main st ny\n"
        "B2,\n"
        "B3,742 Evergreen Tce, Springfield\n"
    )).json()
    by_line = _rows_by_line(data)
    assert by_line[2]["status"] == "invalid"
    assert "业务编号为空" in by_line[2]["reason"]
    assert by_line[3]["status"] == "invalid"
    assert "原始地址为空" in by_line[3]["reason"]
    assert by_line[3]["match"] is None
    assert by_line[4]["status"] == "auto_matched"
    assert data["summary"]["invalid"] == 2


def test_quoted_comma_address_round_trips(client):
    # Comma inside the value must be quoted per CSV rules; then it survives.
    data = client.post("/api/mapping-batches", files=_csv(
        'business_id,address\n'
        'B1,"123 Main Street, New York, NY 10001"\n'
    )).json()
    row = data["rows"][0]
    assert row["raw_address"] == "123 Main Street, New York, NY 10001"
    assert row["status"] == "auto_matched"


# ------------------------------------------------------------- validation


def test_missing_headers_rejected(client):
    r = client.post("/api/mapping-batches", files=_csv("foo,bar\n1,2\n"))
    assert r.status_code == 400
    assert "业务编号" in r.json()["detail"]

    r = client.post("/api/mapping-batches", files=_csv("business_id,foo\n1,2\n"))
    assert r.status_code == 400
    assert "地址" in r.json()["detail"]


def test_more_than_100_rows_rejected(client):
    body = "business_id,address\n" + "".join(
        f"B{i},addr {i} street\n" for i in range(101))
    r = client.post("/api/mapping-batches", files=_csv(body))
    assert r.status_code == 400
    assert "100" in r.json()["detail"]
    # nothing persisted
    assert client.get("/api/mapping-batches").json()["total"] == 0


def test_exactly_100_rows_accepted(client):
    body = "business_id,address\n" + "".join(
        f"B{i},123 main st ny\n" for i in range(100))
    r = client.post("/api/mapping-batches", files=_csv(body))
    assert r.status_code == 200, r.text
    assert r.json()["summary"]["total"] == 100


def test_non_csv_filename_rejected(client):
    r = client.post("/api/mapping-batches",
                    files={"file": ("x.txt", b"business_id,address\n", "text/plain")})
    assert r.status_code == 400


def test_structured_parts_and_header_aliases(client):
    data = client.post("/api/mapping-batches", files=_csv(
        "编号,street,city,state,postal_code\n"
        "B1,123 Main Street,New York,NY,10001\n"
    )).json()
    row = data["rows"][0]
    assert row["business_ref"] == "B1"
    assert row["raw_address"] == "123 Main Street, New York, NY, 10001"
    assert row["status"] == "auto_matched"


# ------------------------------------------------------------ persistence


def test_batch_list_get_and_404(client):
    run = client.post("/api/mapping-batches", files=_csv(
        "business_id,address\nB1,123 main st ny\n")).json()
    fetched = client.get(f"/api/mapping-batches/{run['id']}").json()
    assert fetched["rows"] == run["rows"]
    assert fetched["summary"] == run["summary"]
    assert fetched["table_snapshot"] == run["table_snapshot"]

    listing = client.get("/api/mapping-batches").json()
    assert listing["total"] >= 1
    first = listing["items"][0]
    assert first["id"] == run["id"]
    assert "rows" not in first  # list stays lightweight
    assert first["summary"]["total"] == 1

    assert client.get("/api/mapping-batches/MAP-nope").status_code == 404


# ----------------------------------------------------------------- export


def _parse_export(text: str) -> list[dict]:
    assert text.startswith("﻿")
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    return list(reader)


def test_export_full_mapping_csv(client):
    run = client.post("/api/mapping-batches", files=_csv(
        "business_id,address\n"
        "B1,123 main st ny\n"            # auto
        "B2,1010 Lake Dr Austin TX\n"    # needs review
        "B3,1 Milky Way Mars Base\n"     # no match
        "B4,742 Evergreen Tce\n"
        "B4,50 Hillcrest Rd Dallas\n"    # conflict: both B4 rows blocked
    )).json()
    r = client.get(f"/api/mapping-batches/{run['id']}/export")
    assert r.status_code == 200
    assert "text/csv" in r.headers["content-type"]
    rows = _parse_export(r.text)
    assert len(rows) == 5
    by_ref = {row["业务编号"]: row for row in rows}
    # auto hit: target filled
    assert by_ref["B1"]["目标标准地址ID"]
    assert by_ref["B1"]["匹配状态"] == "自动命中"
    assert "Main Street" in by_ref["B1"]["目标标准地址全文"]
    # needs review / no match: ref kept, target empty, reason present
    assert by_ref["B2"]["目标标准地址ID"] == ""
    assert by_ref["B2"]["匹配状态"] == "待确认"
    assert by_ref["B2"]["说明"]
    assert by_ref["B3"]["目标标准地址ID"] == ""
    assert by_ref["B3"]["匹配状态"] == "无匹配"
    # conflict rows: ref kept, both empty targets, conflict reason
    b4 = [row for row in rows if row["业务编号"] == "B4"]
    assert {row["匹配状态"] for row in b4} == {"编号冲突"}
    assert all(row["目标标准地址ID"] == "" for row in b4)
    assert all("多个不同" in row["说明"] for row in b4)
    # raw address is preserved verbatim for every row
    assert by_ref["B2"]["原始地址"] == "1010 Lake Dr Austin TX"


def test_export_refused_after_table_changes(client):
    run = client.post("/api/mapping-batches", files=_csv(
        "business_id,address\nB1,123 main st ny\n")).json()
    # mutate the standard address table
    client.post("/api/addresses", json={"addresses": [
        {"address": "900 Export Guard Avenue, Austin, TX 73301",
         "external_id": "EXP-GUARD"},
    ]})
    r = client.get(f"/api/mapping-batches/{run['id']}/export")
    assert r.status_code == 409
    assert "快照" in r.json()["detail"]

    # the stored batch now reports the stale snapshot
    fetched = client.get(f"/api/mapping-batches/{run['id']}").json()
    assert fetched["snapshot_current"] is False
    listing = client.get("/api/mapping-batches").json()
    assert listing["items"][0]["snapshot_current"] is False

    # a fresh batch against the changed table exports fine
    fresh = client.post("/api/mapping-batches", files=_csv(
        "business_id,address\nB1,123 main st ny\n")).json()
    assert fresh["snapshot_current"] is True
    assert client.get(f"/api/mapping-batches/{fresh['id']}/export").status_code == 200


def test_export_unknown_batch_404(client):
    r = client.get("/api/mapping-batches/MAP-missing/export")
    assert r.status_code == 404


def test_export_is_complete_not_only_matched(client):
    run = client.post("/api/mapping-batches", files=_csv(
        "business_id,address\n"
        "B1,1 Milky Way Mars Base\n"
        "B2,123 main st ny\n"
    )).json()
    rows = _parse_export(
        client.get(f"/api/mapping-batches/{run['id']}/export").text)
    # both rows present, original order preserved
    assert [r["业务编号"] for r in rows] == ["B1", "B2"]
