import pytest

from sample_tracker import create_app
from sample_tracker.db import get_db


@pytest.fixture
def app(tmp_path):
    return create_app({"TESTING": True, "DATABASE": str(tmp_path / "test.db"), "SECRET_KEY": "test"})


@pytest.fixture
def client(app):
    c = app.test_client()
    c.post("/operator", data={"name": "Tester"})
    return c


def add_party(client, name, party_type="vendor"):
    client.post("/parties/new", data={"name": name, "party_type": party_type})
    with client.application.app_context():
        return get_db().execute("SELECT id FROM parties WHERE name = ?", (name,)).fetchone()["id"]


def receive_new(client, party_id, qty, name="Blue fabric swatch", **extra):
    data = {"sample_id": "new", "new_name": name, "quantity": qty, "party_id": party_id,
            "movement_date": "2026-09-01", **extra}
    return client.post("/receive", data=data, follow_redirects=True)


def one(app, sql, *params):
    with app.app_context():
        return get_db().execute(sql, params).fetchone()


def test_requires_operator_name(app):
    c = app.test_client()
    resp = c.get("/samples")
    assert resp.status_code == 302 and "/operator" in resp.headers["Location"]


def test_all_pages_render(client):
    vendor = add_party(client, "Acme")
    receive_new(client, vendor, 3)
    for url in ["/", "/samples", "/samples/1", "/samples/1/edit", "/samples/new", "/movements",
                "/movements/new?sample_id=1&movement_type=SENT", "/parties", "/parties/1", "/parties/1/edit",
                "/parties/new", "/reports", "/audit", "/receive"]:
        assert client.get(url).status_code == 200, url


def test_receive_creates_sample_with_code_and_stock(client, app):
    vendor = add_party(client, "Acme")
    resp = receive_new(client, vendor, 5)
    assert b"Receipt recorded" in resp.data
    sample = one(app, "SELECT * FROM samples")
    assert sample["code"].startswith("SMP-") and sample["code"].endswith("-0001")
    assert b"5 <small>pcs" in client.get(f"/samples/{sample['id']}").data
    audit = one(app, "SELECT COUNT(*) AS n, MIN(user_name) AS u FROM audit_log WHERE entity IN ('sample','movement')")
    assert audit["n"] == 2 and audit["u"] == "Tester"


def test_receive_and_redirect_immediately(client, app):
    vendor = add_party(client, "Acme")
    customer = add_party(client, "Globex", "customer")
    receive_new(client, vendor, 10, redirect_to=customer, redirect_quantity=4, redirect_tracking="1Z999")
    redirect = one(app, "SELECT * FROM movements WHERE movement_type = 'REDIRECTED'")
    receipt = one(app, "SELECT * FROM movements WHERE movement_type = 'RECEIVED'")
    assert redirect["quantity"] == 4 and redirect["party_id"] == customer
    assert redirect["source_movement_id"] == receipt["id"] and redirect["tracking_number"] == "1Z999"
    assert b"6 <small>pcs" in client.get("/samples/1").data


def test_cannot_send_more_than_on_hand(client, app):
    vendor = add_party(client, "Acme")
    customer = add_party(client, "Globex", "customer")
    receive_new(client, vendor, 2)
    resp = client.post("/movements/new", data={"movement_type": "SENT", "sample_id": 1, "quantity": 3,
                                              "party_id": customer}, follow_redirects=True)
    assert b"only 2 on hand" in resp.data
    assert one(app, "SELECT COUNT(*) AS n FROM movements WHERE movement_type = 'SENT'")["n"] == 0


def test_failed_redirect_rolls_back_receipt(client, app):
    vendor = add_party(client, "Acme")
    customer = add_party(client, "Globex", "customer")
    resp = receive_new(client, vendor, 2, redirect_to=customer, redirect_quantity=5)
    assert b"left to redirect" in resp.data
    assert one(app, "SELECT COUNT(*) AS n FROM movements")["n"] == 0
    assert one(app, "SELECT COUNT(*) AS n FROM samples")["n"] == 0


def test_send_requires_party(client):
    vendor = add_party(client, "Acme")
    receive_new(client, vendor, 2)
    resp = client.post("/movements/new", data={"movement_type": "SENT", "sample_id": 1, "quantity": 1},
                       follow_redirects=True)
    assert b"Select who the sample was sent to" in resp.data


def test_void_keeps_history_and_restores_stock(client, app):
    vendor = add_party(client, "Acme")
    customer = add_party(client, "Globex", "customer")
    receive_new(client, vendor, 5)
    client.post("/movements/new", data={"movement_type": "SENT", "sample_id": 1, "quantity": 5, "party_id": customer})
    sent = one(app, "SELECT id FROM movements WHERE movement_type = 'SENT'")["id"]

    # Receipt can't be voided while its units are out the door.
    resp = client.post("/movements/void", data={"movement_id": 1, "reason": "typo"}, follow_redirects=True)
    assert b"already been sent out" in resp.data

    resp = client.post("/movements/void", data={"movement_id": sent, "reason": "wrong sample"}, follow_redirects=True)
    assert b"voided" in resp.data
    row = one(app, "SELECT * FROM movements WHERE id = ?", sent)
    assert row["voided"] == 1 and row["void_reason"] == "wrong sample" and row["voided_by"] == "Tester"
    assert b"5 <small>pcs" in client.get("/samples/1").data
    assert one(app, "SELECT COUNT(*) AS n FROM audit_log WHERE action = 'void'")["n"] == 1


def test_edit_sample_is_audited(client, app):
    vendor = add_party(client, "Acme")
    receive_new(client, vendor, 1)
    client.post("/samples/1/edit", data={"name": "Red fabric swatch", "unit": "pcs", "storage_location": "B3"})
    entry = one(app, "SELECT details FROM audit_log WHERE action = 'update'")
    assert "Blue fabric swatch" in entry["details"] and "Red fabric swatch" in entry["details"]


def test_party_summary_and_csv(client):
    vendor = add_party(client, "Acme")
    customer = add_party(client, "Globex", "customer")
    receive_new(client, vendor, 8)
    client.post("/movements/new", data={"movement_type": "SENT", "sample_id": 1, "quantity": 3,
                                        "party_id": customer, "movement_date": "2026-09-02"})
    csv_text = client.get("/reports?date_from=2026-01-01&date_to=2026-12-31&format=csv").data.decode()
    assert "Acme,vendor,8,0,0,0" in csv_text and "Globex,customer,0,0,3,0" in csv_text

    export = client.get(f"/movements/export.csv?party_id={customer}").data.decode().splitlines()
    assert len(export) == 2 and "Sent out" in export[1]

    inv = client.get("/samples?format=csv").data.decode()
    assert "Blue fabric swatch" in inv and ",8,3,5," in inv


def test_duplicate_party_rejected(client):
    add_party(client, "Acme")
    resp = client.post("/parties/new", data={"name": "acme", "party_type": "vendor"}, follow_redirects=True)
    assert b"already exists" in resp.data
