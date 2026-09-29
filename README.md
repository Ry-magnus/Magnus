# Sample Tracker

A small web app for tracking physical samples that arrive from vendors and customers and that go out again (sent, redirected or forwarded, returned, or used up). It answers four questions:

- **How many do we have?** Stock on hand for each sample, worked out from its full movement history.
- **Where did it come from?** Every receipt records the vendor or customer, the date, a PO or reference number, the carrier and the tracking number.
- **Where did it go?** Every send-out or redirect records who received it, with shipping details. A redirect can be linked to the receipt it came from.
- **Who did what, and when?** Each entry is stamped with the person who recorded it. An audit trail records every create, edit and void. Nothing is ever deleted.

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python run.py            # open http://localhost:5000
```

To try it with demo data:

```bash
SAMPLE_TRACKER_DB=demo.db python scripts/seed_demo.py
SAMPLE_TRACKER_DB=demo.db python run.py
```

To let other people in the office use it, run `HOST=0.0.0.0 python run.py` and set `SECRET_KEY` to a random value. For a long-running setup, use a WSGI server such as `gunicorn "sample_tracker:create_app()"`.

The data lives in one SQLite file, `instance/samples.db` by default; you can change this with `SAMPLE_TRACKER_DB`. To back up, copy that file.

## How it works

| Concept | What it is |
|---|---|
| **Sample** | One sample record (for example, "Blue cotton swatch, AC-4471"). It gets an automatic code such as `SMP-2026-0001` and can have a category, part/SKU number, unit and storage location. |
| **Vendor/customer** | Anyone samples come from or go to: vendors, customers, internal teams (such as a QA lab), or others. |
| **Movement** | One change in quantity. Stock on hand = everything in − everything out. |

Movement types:

| In (+) | Out (−) |
|---|---|
| Received | Sent out |
| Returned to us | Redirected / forwarded |
| Adjustment (+) | Consumed / tested / disposed |
| | Adjustment (−) |

### Typical workflows

- **Samples arrive:** click **+ Receive**, pick an existing sample or create a new one, choose who sent it, and enter the quantity and reference or tracking details.
- **Samples arrive and are forwarded straight on:** on the same Receive form, open **Redirect** and choose who they are going to. This records both the receipt and the redirect, linked to each other.
- **Sending samples later:** use **Send out**, or **Redirect** on the sample's page. A redirect can be linked to the receipt it came from.
- **Mistakes:** entries are *voided* with a reason, never deleted. A voided entry stays in the history (struck through) but no longer counts toward stock.

### Safeguards

- You can't send out more than you have on hand.
- You can't redirect more of a receipt than is left of it.
- You can't void a receipt whose units have already left.
- If a combined receive-and-redirect fails any check, nothing is saved.

### Screens

- **Dashboard:** totals, this month's in/out, top sources and destinations, and recent activity.
- **Samples:** inventory with received, out and on-hand counts. Each sample has its own page with the full movement trail (including a running balance) and its audit trail.
- **Movements:** every movement, filterable by sample, party, type, direction, date range or text.
- **Vendors & Customers:** what was received from and sent to each one, and their full history.
- **Reports:** totals by type, by month and by party for any date range.
- **Audit trail:** every change, with who made it and when.

Samples, movements and the party summary can all be exported to CSV.

## Development

```bash
pip install -r requirements.txt -r requirements-dev.txt
pytest
```

## Not built yet (possible next steps)

- Real logins. Right now each person types their name when they first open the app, and that name goes on everything they record.
- Barcode or QR labels for sample codes, and scanning them to find a sample.
- Photo or document attachments (packing slips, test reports).
- Email alerts, or reminders for samples that are expected back.
