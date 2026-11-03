from pathlib import Path
import zipfile

from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas


ROOT = Path(__file__).resolve().parents[1]
DEMO_DIR = ROOT / "static" / "oldest-house-test" / "synthetic"
PDF_DIR = DEMO_DIR / "pdfs"
PHOTO_DIR = DEMO_DIR / "photos"
BUNDLE_PATH = ROOT / "SEAI_Synthetic_Demo_PDFs.zip"


def _write_house_svg(path, view, sky, wall, roof):
    details = {
        "front": '<rect x="176" y="188" width="48" height="82" rx="2" fill="#795548"/><circle cx="211" cy="229" r="3" fill="#f3d37a"/><text x="200" y="326" text-anchor="middle">FRONT VIEW</text>',
        "rear": '<rect x="176" y="188" width="48" height="82" rx="2" fill="#795548"/><rect x="100" y="190" width="40" height="38" fill="#a9d8e8"/><rect x="260" y="190" width="40" height="38" fill="#a9d8e8"/><text x="200" y="326" text-anchor="middle">REAR VIEW</text>',
        "gable": '<path d="M92 276V169l108-87 108 87v107" fill="#eadfcf" stroke="#725b48" stroke-width="7"/><path d="M85 169 200 75l115 94" fill="none" stroke="#873f36" stroke-width="15"/><rect x="174" y="197" width="52" height="79" fill="#795548"/><text x="200" y="326" text-anchor="middle">GABLE VIEW</text>',
    }[view]
    path.write_text(
        f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 400 350" role="img" aria-label="Synthetic house illustration">
<rect width="400" height="350" fill="{sky}"/>
<circle cx="330" cy="58" r="24" fill="#f1cf75"/>
<path d="M0 282 Q90 256 178 282 T400 276 V350 H0Z" fill="#88a979"/>
<path d="M86 180 200 85 314 180" fill="none" stroke="{roof}" stroke-width="18" stroke-linejoin="round"/>
<path d="M106 176V280H294V176" fill="{wall}" stroke="#725b48" stroke-width="5"/>
<rect x="128" y="194" width="42" height="40" fill="#a9d8e8" stroke="#725b48" stroke-width="4"/>
<path d="M149 194v40m-21-20h42" stroke="#fff" stroke-width="3"/>
<rect x="230" y="194" width="42" height="40" fill="#a9d8e8" stroke="#725b48" stroke-width="4"/>
<path d="M251 194v40m-21-20h42" stroke="#fff" stroke-width="3"/>
{details}
<text x="200" y="342" text-anchor="middle" font-family="sans-serif" font-size="10" fill="#52615a">SYNTHETIC DEMO ILLUSTRATION</text>
</svg>
""",
        encoding="utf-8",
    )


def _make_pdf(path, title, subtitle, kind):
    pdf = canvas.Canvas(str(path), pagesize=A4, pageCompression=1)
    width, height = A4
    pdf.setTitle(title)
    pdf.setAuthor("Synthetic Demo")
    pdf.setSubject("Fictional training document; contains no real household data.")
    pdf.setFillColorRGB(0.10, 0.20, 0.28)
    pdf.rect(0, height - 122, width, 122, fill=1, stroke=0)
    pdf.setFillColorRGB(1, 1, 1)
    pdf.setFont("Helvetica-Bold", 20)
    pdf.drawString(48, height - 62, title)
    pdf.setFont("Helvetica", 11)
    pdf.drawString(48, height - 86, "Synthetic training sample - not a real SEAI record")
    pdf.setFillColorRGB(0.15, 0.22, 0.28)
    pdf.setFont("Helvetica-Bold", 14)
    pdf.drawString(48, height - 160, subtitle)
    pdf.setFont("Helvetica", 11)
    pdf.drawString(48, height - 188, "Application: DEMO-000001")
    pdf.drawString(48, height - 208, "Property: 1 Example Street, Demo Town")
    pdf.drawString(48, height - 228, "Applicant: Example Owner")
    pdf.setStrokeColorRGB(0.35, 0.48, 0.42)
    pdf.setLineWidth(1.2)
    pdf.line(48, height - 250, width - 48, height - 250)
    if kind == "plan":
        pdf.setFont("Helvetica-Bold", 12)
        pdf.drawString(48, height - 282, "Illustrative floor plan")
        x, y = 110, height - 590
        pdf.rect(x, y, 370, 250, stroke=1, fill=0)
        pdf.line(x + 185, y, x + 185, y + 250)
        pdf.line(x, y + 125, x + 185, y + 125)
        pdf.line(x + 185, y + 150, x + 370, y + 150)
        pdf.setFont("Helvetica", 10)
        pdf.drawCentredString(x + 92, y + 180, "ROOM A")
        pdf.drawCentredString(x + 92, y + 55, "ROOM B")
        pdf.drawCentredString(x + 278, y + 195, "ROOM C")
        pdf.drawCentredString(x + 278, y + 75, "ROOM D")
    else:
        pdf.setFont("Helvetica-Bold", 12)
        pdf.drawString(48, height - 282, "Demo record")
        lines = [
            "Measure: External wall insulation (sample)",
            "Survey quantity: 72.0 m2",
            "Inspection quantity: 72.0 m2",
            "Notes: Synthetic example for application preview and testing.",
            "All names, addresses, identifiers, and quantities on this page are fictional.",
        ]
        for index, line in enumerate(lines):
            pdf.setFont("Helvetica", 11)
            pdf.drawString(48, height - 312 - index * 24, line)
    pdf.setFillColorRGB(0.36, 0.42, 0.44)
    pdf.setFont("Helvetica", 9)
    pdf.drawString(48, 38, "SYNTHETIC DEMO DOCUMENT - DO NOT USE FOR WORKS OR PAYMENT")
    pdf.save()


def create_assets():
    PHOTO_DIR.mkdir(parents=True, exist_ok=True)
    PDF_DIR.mkdir(parents=True, exist_ok=True)
    _write_house_svg(PHOTO_DIR / "front.svg", "front", "#dceaf0", "#eadfcf", "#873f36")
    _write_house_svg(PHOTO_DIR / "rear.svg", "rear", "#e3edf2", "#e4d7c2", "#596d7b")
    _write_house_svg(PHOTO_DIR / "gable.svg", "gable", "#e7edf1", "#eadfcf", "#873f36")

    documents = (
        ("Application", "application-letter.pdf", "Demo Call-Off Summary", "application"),
        ("Survey", "survey-report.pdf", "Synthetic Survey Report", "survey"),
        ("Survey", "survey-sketch.pdf", "Synthetic Survey Sketch", "survey"),
        ("Variation", "floor-plan-1.pdf", "Synthetic Floor Plan - Ground", "plan"),
        ("Variation", "floor-plan-2.pdf", "Synthetic Floor Plan - First", "plan"),
        ("Variation", "floor-plan-3.pdf", "Synthetic Floor Plan - Alternate", "plan"),
    )
    for _, filename, title, kind in documents:
        _make_pdf(PDF_DIR / filename, title, "Fictional application DEMO-000001", kind)

    with zipfile.ZipFile(BUNDLE_PATH, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        for category, filename, _, _ in documents:
            bundle.write(PDF_DIR / filename, f"{category}/{filename}")

    print(f"Created synthetic demo photos, PDFs, and {BUNDLE_PATH.name}.")


if __name__ == "__main__":
    create_assets()
