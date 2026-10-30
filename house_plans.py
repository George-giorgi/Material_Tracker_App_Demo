"""Bounded PDF inspection for the isolated house-plan workspace."""

import re
import shutil

import cv2
import numpy as np
import pymupdf as fitz
import pytesseract
from PIL import Image


MAX_PAGES = 25
MAX_PAGE_TEXT = 20_000
MAX_LABELS_PER_PAGE = 80
OCR_RENDER_SCALE = 1.5
DIMENSION_PATTERN = re.compile(
    r"(?<![\w.])\d{1,5}(?:[.,]\d{1,3})?\s*"
    r"(?:mm|cm|m|metres?|meters?|ft|feet|inches?|in\b|[\"′'])",
    re.IGNORECASE,
)
DIMENSION_PAIR_PATTERN = re.compile(
    r"(?<![\w.])\d{1,5}(?:[.,]\d{1,3})?\s*[x×]\s*"
    r"\d{1,5}(?:[.,]\d{1,3})?(?:\s*(?:mm|cm|m|ft|in))?",
    re.IGNORECASE,
)


class PdfAnalysisError(ValueError):
    """Raised when a floor-plan PDF cannot be inspected safely."""


def _render_grayscale(page):
    scale = min(
        OCR_RENDER_SCALE,
        3500 / max(page.rect.width, page.rect.height),
    )
    pixmap = page.get_pixmap(
        matrix=fitz.Matrix(scale, scale),
        colorspace=fitz.csGRAY,
        alpha=False,
    )
    return cv2.imdecode(
        np.frombuffer(pixmap.tobytes("png"), dtype=np.uint8),
        cv2.IMREAD_GRAYSCALE,
    )


def _page_report(page, page_number, tesseract_available):
    text = page.get_text("text", sort=True)[:MAX_PAGE_TEXT].strip()
    vector_count = len(page.get_drawings())
    image_info = page.get_image_info(xrefs=True)
    page_area = max(page.rect.get_area(), 1)
    image_coverage = min(
        1.0,
        sum(
            max(fitz.Rect(image["bbox"]).get_area(), 0) / page_area
            for image in image_info
            if image.get("bbox")
        ),
    )
    grayscale = None
    ink_ratio = None
    if image_info:
        grayscale = _render_grayscale(page)
        if grayscale is not None:
            thresholded = cv2.adaptiveThreshold(
                grayscale,
                255,
                cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                cv2.THRESH_BINARY,
                31,
                15,
            )
            ink_ratio = round(float(np.count_nonzero(thresholded == 0)) / thresholded.size, 4)

    is_scanned = image_coverage >= 0.6 and vector_count < 3
    ocr_text = ""
    ocr_status = "not-needed"
    if is_scanned:
        if text:
            ocr_status = "embedded-text-available"
        elif not tesseract_available:
            ocr_status = "unavailable-install-tesseract"
        elif grayscale is None:
            ocr_status = "render-failed"
        else:
            processed = cv2.threshold(
                grayscale,
                0,
                255,
                cv2.THRESH_BINARY | cv2.THRESH_OTSU,
            )[1]
            try:
                ocr_text = pytesseract.image_to_string(
                    Image.fromarray(processed),
                    config="--psm 11",
                ).strip()[:MAX_PAGE_TEXT]
                ocr_status = "completed"
            except pytesseract.TesseractError:
                ocr_status = "failed-check-tesseract-installation"

    page_text = text or ocr_text
    labels = [
        re.sub(r"\s+", " ", line).strip()
        for line in page_text.splitlines()
        if re.search(r"[A-Za-z]", line)
        and len(line.strip()) <= 100
    ][:MAX_LABELS_PER_PAGE]
    dimensions = []
    for pattern in (DIMENSION_PATTERN, DIMENSION_PAIR_PATTERN):
        dimensions.extend(
            {
                "text": match.group(0).strip(),
                "page": page_number,
                "source": "pdf-text" if text else "ocr",
                "status": "unverified",
            }
            for match in pattern.finditer(page_text)
        )
    unique_dimensions = {
        (item["text"], item["source"]): item for item in dimensions
    }

    return {
        "page": page_number,
        "width_points": round(page.rect.width, 2),
        "height_points": round(page.rect.height, 2),
        "vector_objects": vector_count,
        "embedded_images": len(image_info),
        "largest_image_coverage": round(image_coverage, 3),
        "scanned_image_likely": is_scanned,
        "opencv_ink_ratio": ink_ratio,
        "text": page_text,
        "labels": labels,
        "dimensions": list(unique_dimensions.values()),
        "ocr_status": ocr_status,
    }


def analyze_pdf(pdf_bytes):
    try:
        document = fitz.open(stream=pdf_bytes, filetype="pdf")
    except (fitz.FileDataError, ValueError, RuntimeError) as exc:
        raise PdfAnalysisError("The uploaded file could not be opened as a PDF.") from exc

    with document:
        if document.needs_pass:
            raise PdfAnalysisError("Password-protected PDFs are not supported.")
        if not 1 <= document.page_count <= MAX_PAGES:
            raise PdfAnalysisError(
                f"PDFs must contain between 1 and {MAX_PAGES} pages."
            )
        tesseract_available = shutil.which("tesseract") is not None
        pages = [
            _page_report(page, page_number, tesseract_available)
            for page_number, page in enumerate(document, start=1)
        ]

    scanned_count = sum(page["scanned_image_likely"] for page in pages)
    pages_with_vectors = sum(page["vector_objects"] > 0 for page in pages)
    if scanned_count == len(pages):
        content_type = "scanned"
    elif scanned_count or (pages_with_vectors and any(page["embedded_images"] for page in pages)):
        content_type = "mixed"
    elif pages_with_vectors or any(page["text"] for page in pages):
        content_type = "vector"
    else:
        content_type = "unknown"

    return {
        "content_type": content_type,
        "page_count": len(pages),
        "ocr_available": tesseract_available,
        "pages": pages,
        "dimensions": [
            dimension
            for page in pages
            for dimension in page["dimensions"]
        ],
        "limitations": [
            "Extracted text, labels, and dimensions are candidates and require review.",
            "Walls, openings, scale, and room geometry have not yet been reconstructed.",
            *(
                ["Scanned-page OCR is unavailable; install the Tesseract executable to enable it."]
                if not tesseract_available and scanned_count
                else []
            ),
        ],
    }


def render_page_png(pdf_bytes, page_number):
    """Render a one-based PDF page as a bounded PNG for plan tracing."""
    try:
        document = fitz.open(stream=pdf_bytes, filetype="pdf")
    except (fitz.FileDataError, ValueError, RuntimeError) as exc:
        raise PdfAnalysisError("The uploaded file could not be opened as a PDF.") from exc
    with document:
        if document.needs_pass:
            raise PdfAnalysisError("Password-protected PDFs are not supported.")
        if not 1 <= page_number <= min(document.page_count, MAX_PAGES):
            raise PdfAnalysisError("The requested plan page does not exist.")
        page = document[page_number - 1]
        scale = min(1.75, 3200 / max(page.rect.width, page.rect.height))
        try:
            pixmap = page.get_pixmap(
                matrix=fitz.Matrix(scale, scale),
                colorspace=fitz.csRGB,
                alpha=False,
            )
        except (fitz.FileDataError, RuntimeError, ValueError) as exc:
            raise PdfAnalysisError("This PDF page could not be rendered.") from exc
        return pixmap.tobytes("png")


def suggest_exterior_outline(pdf_bytes, page_number):
    """Suggest one likely closed footprint; results must be reviewed by a person."""
    image_bytes = render_page_png(pdf_bytes, page_number)
    image = cv2.imdecode(np.frombuffer(image_bytes, dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
    if image is None:
        raise PdfAnalysisError("This PDF page could not be analyzed for an outline.")

    height, width = image.shape
    binary = cv2.threshold(
        image, 0, 255, cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU
    )[1]
    kernel_size = max(3, round(min(width, height) / 400))
    kernel = cv2.getStructuringElement(
        cv2.MORPH_RECT, (kernel_size, kernel_size)
    )
    connected = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, kernel)
    contours, _ = cv2.findContours(
        connected, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE
    )

    candidates = []
    page_area = width * height
    for contour in contours:
        area = cv2.contourArea(contour)
        if not 0.025 * page_area <= area <= 0.8 * page_area:
            continue
        x, y, box_width, box_height = cv2.boundingRect(contour)
        if (
            box_width < 0.12 * width
            or box_height < 0.12 * height
            or x < 0.015 * width
            or y < 0.015 * height
            or x + box_width > 0.985 * width
            or y + box_height > 0.985 * height
        ):
            continue
        perimeter = cv2.arcLength(contour, True)
        polygon = cv2.approxPolyDP(contour, max(2, 0.006 * perimeter), True)
        if not 4 <= len(polygon) <= 32:
            continue
        candidates.append((area, polygon.reshape(-1, 2)))

    if not candidates:
        return {"width": width, "height": height, "points": []}

    _, points = max(candidates, key=lambda candidate: candidate[0])
    return {
        "width": width,
        "height": height,
        "points": [
            {"x": round(float(x) / width, 6), "y": round(float(y) / height, 6)}
            for x, y in points
        ],
    }
