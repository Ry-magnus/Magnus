"""Fill a database with demo data so you can try the app.

    SAMPLE_TRACKER_DB=demo.db python scripts/seed_demo.py
    SAMPLE_TRACKER_DB=demo.db python run.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sample_tracker import create_app  # noqa: E402
from sample_tracker import services as svc  # noqa: E402
from sample_tracker.db import get_db  # noqa: E402

app = create_app()
with app.app_context():
    if get_db().execute("SELECT COUNT(*) FROM samples").fetchone()[0]:
        sys.exit("Database already has data; point SAMPLE_TRACKER_DB at a fresh file.")
    u = "Demo"
    acme = svc.create_party({"name": "Acme Textiles", "party_type": "vendor", "contact_person": "Priya Shah", "email": "priya@acme.example"}, u)
    zen = svc.create_party({"name": "Zenith Components", "party_type": "vendor"}, u)
    globex = svc.create_party({"name": "Globex Retail", "party_type": "customer", "contact_person": "Sam Lee"}, u)
    initech = svc.create_party({"name": "Initech", "party_type": "customer"}, u)
    lab = svc.create_party({"name": "QA Lab", "party_type": "internal"}, u)

    fabric = svc.create_sample({"name": "Blue cotton swatch 40x40", "category": "Fabric", "part_number": "AC-4471", "unit": "pcs", "storage_location": "Shelf B3"}, u)
    pcb = svc.create_sample({"name": "Controller PCB rev C", "category": "Electronics", "part_number": "ZC-PCB-C", "unit": "pcs", "storage_location": "ESD cabinet 1"}, u)
    box = svc.create_sample({"name": "Kraft mailer box M", "category": "Packaging", "unit": "pcs"}, u)

    mv = lambda **kw: svc.record_movement(kw, u)  # noqa: E731
    r1 = mv(sample_id=fabric, movement_type="RECEIVED", quantity=20, party_id=acme, movement_date="2026-07-03", reference="PO-1182", carrier="DHL", tracking_number="JD014600003")
    mv(sample_id=fabric, movement_type="REDIRECTED", quantity=8, party_id=globex, movement_date="2026-07-04", source_movement_id=r1, carrier="FedEx", tracking_number="7749 1200 3301", notes="Customer approval set")
    mv(sample_id=fabric, movement_type="SENT", quantity=3, party_id=lab, movement_date="2026-07-10", notes="Colour-fastness test")
    mv(sample_id=fabric, movement_type="CONSUMED", quantity=2, movement_date="2026-07-20", notes="Destroyed in wash test")
    r2 = mv(sample_id=pcb, movement_type="RECEIVED", quantity=6, party_id=zen, movement_date="2026-08-12", reference="RFQ-77")
    mv(sample_id=pcb, movement_type="REDIRECTED", quantity=6, party_id=initech, movement_date="2026-08-12", source_movement_id=r2, carrier="Courier", notes="Forwarded same day")
    mv(sample_id=pcb, movement_type="RETURNED", quantity=2, party_id=initech, movement_date="2026-09-05", notes="Two returned after eval")
    mv(sample_id=box, movement_type="RECEIVED", quantity=50, party_id=acme, movement_date="2026-09-15", reference="PO-1240")
    mv(sample_id=box, movement_type="SENT", quantity=10, party_id=globex, movement_date="2026-09-22")
    get_db().commit()
    print("Demo data loaded.")
