"""Seed synthetic SEAI-shaped measure details for the ten demo houses."""
import json
import os
import re
import sqlite3
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

import server


DEFAULT_PDF_ZIP = Path(__file__).with_name("WH570150.zip")
PDF_CATEGORIES = ("Application", "Survey", "Variation")
HOUSE_ID_PATTERN = re.compile(r"DEMO-FIC-\d{3}\Z")
MEASURE_FIELDS = (
    "Name",
    "Survey Quantity",
    "Survey Qty including Variation",
    "Inspection Quantity",
    "Invoice Quantity",
)


def _sample_measures(index):
    radiator_count = 5 + index % 4
    attic_area = f"{35 + index * 2.5:.1f}"
    measures = [
        {
            "Name": "Gas boiler and controls (Basic & controls pack)",
            "Survey Quantity": "0",
            "Survey Qty including Variation": "1",
            "Inspection Quantity": "1",
            "Invoice Quantity": "1",
        },
        {
            "Name": "Single coil hot water cylinder with anode (part L compliant)",
            "Survey Quantity": "0",
            "Survey Qty including Variation": "1",
            "Inspection Quantity": "1",
            "Invoice Quantity": "1",
        },
        {
            "Name": "Radiator",
            "Survey Quantity": str(radiator_count),
            "Survey Qty including Variation": str(radiator_count),
            "Inspection Quantity": str(radiator_count),
            "Invoice Quantity": str(radiator_count),
        },
        {
            "Name": "TRV",
            "Survey Quantity": str(radiator_count - 1),
            "Survey Qty including Variation": str(radiator_count - 1),
            "Inspection Quantity": str(radiator_count - 1),
            "Invoice Quantity": str(radiator_count - 1),
        },
        {
            "Name": "LED Bulbs: supply only (4 no.)",
            "Survey Quantity": "1",
            "Survey Qty including Variation": "1",
            "Inspection Quantity": "1",
            "Invoice Quantity": "1",
        },
        {
            "Name": "Air Tightness Testing",
            "Survey Quantity": "1",
            "Survey Qty including Variation": "1",
            "Inspection Quantity": "1",
            "Invoice Quantity": "1",
        },
        {
            "Name": "Attic (Loft) Insulation 100 mm top-up",
            "Survey Quantity": attic_area,
            "Survey Qty including Variation": attic_area,
            "Inspection Quantity": attic_area,
            "Invoice Quantity": attic_area,
        },
    ]
    if index % 2 == 0:
        wall_area = f"{72 + index * 3.5:.1f}"
        measures.append(
            {
                "Name": "External Wall Insulation",
                "Survey Quantity": wall_area,
                "Survey Qty including Variation": wall_area,
                "Inspection Quantity": wall_area,
                "Invoice Quantity": wall_area,
            }
        )
    return measures


def _sample_variances(index):
    wall_variation = 3 + index
    return [
        {
            "Variance ID": f"DEMO-VAR-{index:03d}-01",
            "Measure": "Attic (Loft) Insulation 100 mm top-up",
            "Value": str(2 + index % 4),
            "Comment": "Synthetic demo note: additional area confirmed during sample check.",
        },
        {
            "Variance ID": f"DEMO-VAR-{index:03d}-02",
            "Measure": "External Wall Insulation",
            "Value": str(wall_variation if index % 2 == 0 else -wall_variation),
            "Comment": "Synthetic demo note: sample wall quantity adjusted after review.",
        },
        {
            "Variance ID": f"DEMO-VAR-{index:03d}-03",
            "Measure": "TRV",
            "Value": "1",
            "Comment": "Synthetic demo note: one additional valve included in the sample.",
        },
    ]


def _sample_people_and_property(index, house):
    address_line, _, town = (house["address"] or "").partition(", ")
    start_day = 1 + index
    return {
        "Application Details": {
            "Application ID": f"DEMO-WH{index:06d}",
            "Modified Date": f"{start_day:02d}/10/2026 09:00:00",
            "Applicant Name": house["owner_name"] or f"Fictional Owner {index:02d}",
            "Mobile Phone": f"080 000 {index:04d}",
            "Primary Phone": "",
            "Secondary Phone": "",
            "Email": f"owner{index:02d}@example.invalid",
            "Date Signed": f"{start_day:02d}/08/2026",
            "Confirmation of Eligibility": "Yes",
            "Proof of Ownership": "Yes",
            "Application Status": "A140 - Works accepted",
            "Application Programme": "WHS 2.0",
            "Install End Date": "",
            "Lot Number": "Demo lot",
            "Lot HE": "Demo",
        },
        "Property Details": {
            "MPRN": f"99000000{index:03d}",
            "Address Line 1": address_line,
            "Address Line 2": "",
            "Address Line 3": "",
            "Address Line 4": "",
            "GPS Latitude": "",
            "GPS Longitude": "",
            "City/Town": town,
            "County": "Demo County",
            "Eircode": f"D{index:02d} DEMO",
            "Post-Install BER": "",
        },
        "Contractor Project Manager": {
            "PM First Name": "Demo",
            "PM Last Name": f"Manager {index:02d}",
            "PM Email Address": f"pm{index:02d}@example.invalid",
            "Estimated Start Date": f"{start_day:02d}/10/2026",
            "Actual Start Date": "",
            "Estimated Completion Date": f"{start_day:02d}/12/2026",
        },
        "QA Manager": {
            "QA Same as PM": "No",
            "QA First Name": "Demo",
            "QA Last Name": f"Assurance {index:02d}",
            "QA Email Address": f"qa{index:02d}@example.invalid",
        },
    }


def _sample_notes(index):
    day = 1 + index
    return [
        {
            "Note Type": "Notes (Heating)",
            "Added On": f"{day:02d}/09/2026 10:00",
            "Added By": "Demo Survey",
            "Note": "Synthetic demo note: sample heating system comment.",
        },
        {
            "Note Type": "Notes (Roof)",
            "Added On": f"{day:02d}/09/2026 10:05",
            "Added By": "Demo Survey",
            "Note": "Synthetic demo note: sample roof insulation comment.",
        },
        {
            "Note Type": "Notes (Walls)",
            "Added On": f"{day:02d}/09/2026 10:10",
            "Added By": "Demo Survey",
            "Note": "Synthetic demo note: sample wall construction comment.",
        },
        {
            "Note Type": "General",
            "Added On": f"{day + 5:02d}/10/2026 09:30",
            "Added By": "Demo Contractor",
            "Note": "Synthetic demo note: sample follow-up comment.",
        },
    ]


def seed_seai_measure_demo():
    with server.connect_db() as db:
        tables = {
            row["name"]
            for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        missing_tables = {"seai_applications", "seai_measures"} - tables
        if missing_tables:
            raise RuntimeError(
                "The target database is missing SEAI tables: "
                + ", ".join(sorted(missing_tables))
            )

        houses = db.execute(
            """
            SELECT id, address, owner_name FROM provided_houses
            WHERE source = 'fictional_sample'
              AND id GLOB 'DEMO-FIC-[0-9][0-9][0-9]'
            ORDER BY id
            """
        ).fetchall()
        if len(houses) != 10 or any(
            not HOUSE_ID_PATTERN.fullmatch(house["id"]) for house in houses
        ):
            raise RuntimeError(
                "Expected exactly 10 fictional DEMO-FIC houses in the target database."
            )

        house_ids = [house["id"] for house in houses]
        house_rows = {house["id"]: house for house in houses}
        existing = {
            row["application_id"]: row["detail_data_json"]
            for row in db.execute(
                """
                SELECT application_id, detail_data_json
                FROM seai_applications
                WHERE application_id IN ({})
                """.format(",".join("?" for _ in house_ids)),
                house_ids,
            )
        }
        for house_id, raw_details in existing.items():
            try:
                details = json.loads(raw_details)
            except (TypeError, json.JSONDecodeError) as exc:
                raise RuntimeError(
                    f"Refusing to overwrite invalid SEAI details for {house_id}."
                ) from exc
            if not isinstance(details, dict) or details.get("_demo_data") is not True:
                raise RuntimeError(
                    f"Refusing to overwrite non-demo SEAI details for {house_id}."
                )

        retrieved_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        for index, house_id in enumerate(house_ids, start=1):
            measures = _sample_measures(index)
            variances = _sample_variances(index)
            details = {
                "_demo_data": True,
                **_sample_people_and_property(index, house_rows[house_id]),
                "Measures": measures,
                "Variance": variances,
                "Notes": _sample_notes(index),
            }
            db.execute(
                """
                INSERT INTO seai_applications (
                    application_id, detail_path, rag_status, application_status,
                    application_programme, lot_number, allocation_date,
                    application_date, list_data_json, detail_data_json, retrieved_at
                )
                VALUES (?, ?, 'Red', 'A140 - Works accepted', 'WHS 2.0',
                        'Demo lot', '', '', ?, ?, ?)
                ON CONFLICT(application_id) DO UPDATE SET
                    detail_path = excluded.detail_path,
                    rag_status = excluded.rag_status,
                    application_status = excluded.application_status,
                    application_programme = excluded.application_programme,
                    lot_number = excluded.lot_number,
                    allocation_date = excluded.allocation_date,
                    application_date = excluded.application_date,
                    list_data_json = excluded.list_data_json,
                    detail_data_json = excluded.detail_data_json,
                    retrieved_at = excluded.retrieved_at
                """,
                (
                    house_id,
                    f"demo://application/{house_id}",
                    json.dumps({"_demo_data": True}, ensure_ascii=False),
                    json.dumps(details, ensure_ascii=False),
                    retrieved_at,
                ),
            )
            db.execute(
                "DELETE FROM seai_measures WHERE application_id = ?",
                (house_id,),
            )
            db.executemany(
                """
                INSERT INTO seai_measures (
                    application_id, row_number, name, survey_quantity,
                    survey_quantity_including_variation, inspection_quantity,
                    invoice_quantity
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        house_id,
                        row_number,
                        *(measure[field] for field in MEASURE_FIELDS),
                    )
                    for row_number, measure in enumerate(measures, start=1)
                ],
            )

    return len(house_ids)


def read_pdf_bundle(zip_path):
    """Return (category, filename, bytes) for each PDF in a portal download zip."""
    entries = []
    with zipfile.ZipFile(zip_path) as bundle:
        for info in sorted(bundle.infolist(), key=lambda item: item.filename):
            parts = PurePosixPath(info.filename).parts
            if info.is_dir() or len(parts) < 2 or not parts[-1].lower().endswith(".pdf"):
                continue
            if parts[0] not in PDF_CATEGORIES:
                continue
            entries.append((parts[0], parts[-1], bundle.read(info)))
    return entries


def seed_seai_demo_pdfs(zip_path):
    pdfs = read_pdf_bundle(zip_path)
    if not pdfs:
        raise RuntimeError(f"No Application/Survey/Variation PDFs found in {zip_path}.")
    with server.connect_db() as db:
        house_ids = [
            row["application_id"]
            for row in db.execute(
                "SELECT application_id FROM seai_applications "
                "WHERE application_id GLOB 'DEMO-FIC-[0-9][0-9][0-9]' ORDER BY 1"
            )
        ]
        for house_id in house_ids:
            db.execute(
                "DELETE FROM seai_documents WHERE application_id = ? "
                f"AND filename IN ({','.join('?' for _ in pdfs)})",
                (house_id, *(filename for _, filename, _ in pdfs)),
            )
            db.executemany(
                """
                INSERT INTO seai_documents
                    (application_id, category, filename, extension, downloaded, content)
                VALUES (?, ?, ?, '.pdf', 1, ?)
                """,
                [
                    (house_id, category, filename, sqlite3.Binary(content))
                    for category, filename, content in pdfs
                ],
            )
    return len(house_ids), len(pdfs)


if __name__ == "__main__":
    if not os.environ.get("EWI_DB_PATH"):
        raise SystemExit(
            "Set EWI_DB_PATH to the intended demo database; refusing the default database."
        )
    count = seed_seai_measure_demo()
    print(
        f"Seeded synthetic SEAI measure details for {count} fictional houses "
        f"in {server.DB_PATH}."
    )
    pdf_zip = Path(os.environ.get("SEAI_DEMO_PDF_ZIP") or DEFAULT_PDF_ZIP)
    if pdf_zip.is_file():
        houses, files = seed_seai_demo_pdfs(pdf_zip)
        print(f"Attached {files} PDFs from {pdf_zip.name} to {houses} houses.")
    else:
        print(f"PDF bundle not found ({pdf_zip}); skipped PDF attachments.")
