# DentScan: vehicle damage detection and repair estimates

Upload photos of a damaged car. A YOLOv8 model marks the damaged panels (bonnet, bumper, dickey, door, fender, light, windshield), prices the repair for the owner's make and model, and turns the estimate into an insurance claim that an assessor approves or rejects.

## Quick start

```bash
# Windows
run.bat

# macOS / Linux
./run.sh
```

Or by hand:

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows   (macOS/Linux: source .venv/bin/activate)
pip install -r requirements.txt
python app.py
```

Open http://127.0.0.1:5000. The database is created (or upgraded) automatically on start.

- **Customer:** create an account from the home page.
- **Assessor/admin:** http://127.0.0.1:5000/admin/login, default `admin@gmail.com` / `admin123`. Change it under **Settings** the first time you log in.

Want to look around without installing PyTorch? Run with simulated detections:

```bash
# Windows: set DETECTOR_MODE=demo && python app.py
DETECTOR_MODE=demo python app.py
```

## What's in it

**For customers**
- Multi-photo estimates: up to 6 photos per claim, analysed together. A panel that shows up in several photos is counted once.
- Interactive photo viewer: damage marks drawn over the photo; hover a mark, a line item or a panel on the car diagram and the other two light up. Toggle marks on and off, download the marked photo.
- Damage map: a side-view diagram of the car with the damaged panels hatched.
- Editable estimate: switch each line between repair and replace, change quantity, untick false detections, add a part the model missed. Totals update live before you save.
- Itemised pricing: part or repair cost, labour, GST, insurer share (cover % minus deductible) and what you pay.
- Detection sensitivity slider per estimate.
- Claim submission with RC book and policy (PDF or photo). The policy number is read from text PDFs automatically, or with OCR when installed; customers can also type it.
- Claim history timeline, assessor notes, printable / save-as-PDF estimate.
- Profile editing (including the vehicle) and password change.
- Light and dark themes, works on phones.

**For assessors (admin)**
- Overview: claims awaiting a decision, approved payouts, estimates per day, a heat map of which panels get damaged most.
- Claims list with status filters, search, pagination and CSV export.
- Claim review: adjust line items, set cover % and deductible, approve, or reject with a reason the customer sees. Reopen decided claims.
- Customers: search, suspend/restore, delete (claims are kept for audit).
- Parts price list editor: add, edit or remove makes and models.
- Settings: repair %, labour %, GST, default cover, deductible, detection threshold, vehicle check, admin password.

## Configuration

Copy `.env.example` to `.env`. Useful settings:

| Variable | Default | Purpose |
|---|---|---|
| `SECRET_KEY` | generated in `instance/` | Session signing key |
| `ADMIN_EMAIL` / `ADMIN_PASSWORD` | `admin@gmail.com` / `admin123` | First admin account |
| `APP_NAME` | `DentScan` | Name shown in the UI |
| `DAMAGE_WEIGHTS` | `models/best.pt` | Damage model |
| `VEHICLE_WEIGHTS` | `yolov8n.pt` | Vehicle check model, downloaded automatically; set empty to disable |
| `DETECTOR_MODE` | `auto` | `demo` simulates detections |

**OCR for scanned policies** (optional, large download): `pip install easyocr pymupdf`.

## Project layout

```
app.py              routes and views
database.py         SQLite schema, migrations, settings, price lookup
detector.py         YOLOv8 damage detection, vehicle check, photo annotation
estimator.py        severity, repair/replace suggestions, pricing and totals
documents.py        policy number extraction (PDF text / OCR)
templates/          Jinja templates (admin/ holds the assessor console)
static/css, js      design system and front-end behaviour
models/best.pt      trained damage model (7 classes)
car_parts_prices.json  initial parts price list
instance/           uploaded photos and documents, secret key (created at run time)
train.py, dataset/  separate ResNet50 damaged/not-damaged classifier experiment (not used by the app)
```

## Upgrading from the previous version

Keep your `car_damage_detection.db` in the project folder. On first start the old `reports` table is converted into the new claims format (renamed to `reports_legacy`, nothing is deleted) and the old photos under `static/uploads/` keep working.

## Notes

- Estimates are indicative. Severity is a heuristic from how much of the photo the damaged part fills and the model's confidence, so it is shown as a suggestion that customers and assessors can change.
- Uploaded documents are stored in `instance/` and served only to the claim owner and admins.
- The built-in server is for development. For deployment use a WSGI server, e.g. `waitress-serve --port 5000 app:app` (Windows) or `gunicorn -w 1 --threads 4 app:app`.
