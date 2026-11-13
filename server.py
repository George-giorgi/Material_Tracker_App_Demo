import base64
import binascii
import csv
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import sqlite3
import ssl
import time
from threading import Lock
from urllib.error import URLError
from urllib.request import Request as UrlRequest, urlopen
import zipfile
from contextlib import asynccontextmanager, contextmanager
from datetime import date, datetime, timedelta, timezone
from http.cookies import CookieError, SimpleCookie
from io import BytesIO, StringIO
from xml.etree.ElementTree import ParseError, fromstring, tostring
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse
from xml.sax.saxutils import escape

import uvicorn
from fastapi import FastAPI, Request as FastAPIRequest
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response

import pymupdf as fitz
from PIL import Image, ImageOps
from pillow_heif import register_heif_opener
from pypdf.errors import PdfReadError
from pypdf import PdfReader, PdfWriter
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
import reportlab

register_heif_opener(thumbnails=False)


ROOT = Path(__file__).resolve().parent
STATIC_DIR = ROOT / "static"
SURVEY_ROOM_TEST_PDFS = {
    "application-letter.pdf": ("Application", "Application letter"),
    "survey-report.pdf": ("Survey", "Survey report"),
    "survey-sketch.pdf": ("Survey", "Survey sketch"),
    "floor-plan-1.pdf": ("Variation", "Floor plan 1"),
    "floor-plan-2.pdf": ("Variation", "Floor plan 2"),
    "floor-plan-3.pdf": ("Variation", "Floor plan 3"),
}
DB_PATH = Path(os.environ.get("EWI_DB_PATH", ROOT / "instance" / "ewi.sqlite3"))
PORT = int(os.environ.get("EWI_PORT", "8001"))
HOST = os.environ.get("EWI_HOST", "127.0.0.1")
TLS_CERT = os.environ.get("EWI_TLS_CERT", "")
TLS_KEY = os.environ.get("EWI_TLS_KEY", "")
COOKIE_SECURE = bool(TLS_CERT and TLS_KEY)
TRUST_CLOUDFLARE_PROXY = (
    os.environ.get("EWI_TRUST_CLOUDFLARE_PROXY", "").strip().lower()
    in {"1", "true", "yes", "on"}
)
SESSION_MAX_AGE = 12 * 60 * 60
CREDENTIAL_ITERATIONS = 310_000
HOUSE_DOCUMENT_MAX_BYTES = 8_000_000
HOUSE_DOCUMENT_MAX_COUNT = 25
HOUSE_VENT_PHOTO_MAX_BYTES = 8_000_000
HOUSE_VENT_PHOTO_MAX_COUNT = 25
HOUSE_PLUMBING_PHOTO_MAX_BYTES = HOUSE_VENT_PHOTO_MAX_BYTES
HOUSE_PLUMBING_PHOTO_MAX_COUNT = HOUSE_VENT_PHOTO_MAX_COUNT
HOUSE_DOCUMENT_MAX_PAGES = 300
HOUSE_DOCUMENT_MAX_ANNOTATIONS = 500
HOUSE_DOCUMENT_MAX_ANNOTATION_POINTS = 1000
VENT_ANNOTATION_ICON_FILES = {
    "trickle-vent": "Rytons Window Trickle Ventilators.svg",
    "controllable-panel": "Rytons controllable LookRyt panel.svg",
    "terracotta-grille": "Rytons Controllable LookRyt AirCore Terracotta external grille.svg",
    "high-rise-grille": "Rytons Controllable High Rise LookRyt AirCore Buff-sand external grille.svg",
    "wall-ventilator": "INTERVENT NVF Range 100mm 4in and 150mm 6in.svg",
    "humidity-ventilator": "HUMIDITY-controlled ventilation SYSTEMS.svg",
    "roof-ventilator": "G RANGE TILE & SLATE VENTILATORS roof.svg",
    "axial-fan": "D Series Axial Fan.svg",
    "core-drill": "5” Core Drill Ventilator.svg",
}
SEAI_DOCUMENT_MAX_BYTES = 20_000_000
MOVEMENT_TYPES = {"collection", "pickup", "delivery", "use", "return", "transfer"}
LOCATION_TYPES = {"warehouse", "team", "house"}
MET_EIREANN_COUNTY_FORECAST_URL = (
    "https://www.met.ie/Open_Data/xml/county_forecast.xml"
)
MET_EIREANN_WEATHER_CACHE_SECONDS = 15 * 60
_met_eireann_weather_cache = None
_met_eireann_weather_cache_expiry = 0
_met_eireann_weather_lock = Lock()
_change_revision_lock = Lock()
_change_revisions = {
    "dashboard": 0,
    "documents": 0,
    "movements": 0,
    "users": 0,
}


def _record_change(path):
    if path in {
        "/api/house-documents",
        "/api/seai-documents",
        "/api/seai-documents/save-selected",
    } or re.fullmatch(
        r"/api/(?:house|seai)-documents/\d+/(?:annotations|vent-annotations|delete-selected)",
        path,
    ) or re.fullmatch(
        r"/api/survey-room-test-pdfs/[^/]+/annotations", path
    ):
        domain = "documents"
    elif path in {"/api/seai-documents/share"} or re.fullmatch(
        r"/api/house-documents/\d+/share|/api/seai-documents/\d+/share", path
    ):
        return
    elif path == "/api/auth/setup":
        domain = "users"
    elif path.startswith(("/api/users", "/api/roles", "/api/departments")):
        domain = "users"
    elif path.startswith("/api/auth/"):
        return
    else:
        domain = "dashboard"

    with _change_revision_lock:
        _change_revisions[domain] += 1
        if path in {"/api/movements", "/api/movements/batch"}:
            _change_revisions["movements"] += 1


def _change_revision_snapshot():
    with _change_revision_lock:
        return dict(_change_revisions)


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def _parse_met_eireann_county_forecast(content):
    try:
        root = fromstring(content)
    except ParseError as exc:
        raise ApiError(502, "Met Éireann returned an unreadable forecast.") from exc
    if root.tag != "forecast":
        raise ApiError(502, "Met Éireann returned an unexpected forecast format.")

    wanted_counties = {"MAYO": "Mayo", "SLIGO": "Sligo"}
    areas = []
    for county in root.findall("county"):
        county_name = (county.findtext("name") or "").strip().upper()
        if county_name not in wanted_counties:
            continue
        days = []
        for forecast_day in county.findall("day"):
            forecast_date = (forecast_day.findtext("date") or "").strip()
            try:
                datetime.fromisoformat(forecast_date)
            except ValueError as exc:
                raise ApiError(
                    502, "Met Éireann returned an invalid forecast date."
                ) from exc

            def number(field):
                raw_value = forecast_day.findtext(field)
                if raw_value is None or not raw_value.strip():
                    return None
                try:
                    value = float(raw_value)
                except ValueError as exc:
                    raise ApiError(
                        502, "Met Éireann returned invalid forecast values."
                    ) from exc
                return value if math.isfinite(value) else None

            rainfall_day = number("rainfall_6_18")
            rainfall_night = number("rainfall_18_6")
            days.append(
                {
                    "date": forecast_date[:10],
                    "weather": (forecast_day.findtext("weather") or "").strip(),
                    "min_temp": number("min_temp"),
                    "max_temp": number("max_temp"),
                    "wind_speed_kmh": number("wind_speed"),
                    "wind_direction": (forecast_day.findtext("wind_dir") or "").strip(),
                    "rainfall_mm": (
                        rainfall_day + rainfall_night
                        if rainfall_day is not None and rainfall_night is not None
                        else None
                    ),
                }
            )
        if days:
            areas.append(
                {"county": wanted_counties[county_name], "days": days}
            )

    if {area["county"] for area in areas} != {"Mayo", "Sligo"}:
        raise ApiError(
            502, "The Mayo and Sligo county forecasts are temporarily unavailable."
        )
    return {
        "issued": root.attrib.get("issued"),
        "areas": areas,
        "source": "Met Éireann Open Data",
        "source_url": "https://www.met.ie/",
    }


def _met_eireann_county_forecast():
    global _met_eireann_weather_cache, _met_eireann_weather_cache_expiry
    with _met_eireann_weather_lock:
        now = time.monotonic()
        if (
            _met_eireann_weather_cache is not None
            and now < _met_eireann_weather_cache_expiry
        ):
            return _met_eireann_weather_cache

        request = UrlRequest(
            MET_EIREANN_COUNTY_FORECAST_URL,
            headers={"User-Agent": "MaterialTracker/1.0"},
        )
        try:
            with urlopen(request, timeout=8) as response:
                content = response.read(256_001)
        except (URLError, TimeoutError, OSError) as exc:
            raise ApiError(
                502, "Met Éireann weather is temporarily unavailable."
            ) from exc
        if len(content) > 256_000:
            raise ApiError(502, "Met Éireann returned an oversized forecast.")

        forecast = _parse_met_eireann_county_forecast(content)
        _met_eireann_weather_cache = forecast
        _met_eireann_weather_cache_expiry = (
            time.monotonic() + MET_EIREANN_WEATHER_CACHE_SECONDS
        )
        return forecast


def _open_pdf_document(content):
    try:
        document = fitz.open(stream=content, filetype="pdf")
    except (fitz.FileDataError, ValueError, RuntimeError) as exc:
        raise ApiError(422, "This uploaded file could not be read as a PDF.") from exc
    if document.needs_pass:
        document.close()
        raise ApiError(422, "Password-protected PDFs cannot be annotated.")
    if not document.page_count:
        document.close()
        raise ApiError(422, "This PDF does not contain any pages.")
    if document.page_count > HOUSE_DOCUMENT_MAX_PAGES:
        document.close()
        raise ApiError(
            422,
            f"PDFs with more than {HOUSE_DOCUMENT_MAX_PAGES} pages cannot be annotated.",
        )
    return document


def _render_pdf_page(content, page_number):
    document = _open_pdf_document(content)
    try:
        if page_number < 1 or page_number > document.page_count:
            raise ApiError(404, "That PDF page does not exist.")
        page = document.load_page(page_number - 1)
        rect = page.rect
        scale = min(1.6, 1800 / max(rect.width, rect.height))
        pixmap = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
        return pixmap.tobytes("png"), document.page_count
    except ApiError:
        raise
    except (fitz.FileDataError, ValueError, RuntimeError) as exc:
        raise ApiError(422, "This PDF page could not be rendered.") from exc
    finally:
        document.close()


def _render_ventilation_icon(
    icon_key, rotation, base_width, base_height, browser_image=None
):
    filename = VENT_ANNOTATION_ICON_FILES.get(icon_key)
    if not filename:
        raise ApiError(400, "A ventilation icon is not recognized.")
    radians = math.radians(rotation)
    cosine = abs(math.cos(radians))
    sine = abs(math.sin(radians))
    rotated_width = base_width * cosine + base_height * sine
    rotated_height = base_height * cosine + base_width * sine
    if browser_image is not None:
        image_width = max(1, round(base_width * 3))
        image_height = max(1, round(base_height * 3))
        raster = browser_image.resize(
            (image_width, image_height),
            Image.Resampling.LANCZOS,
        )
        if rotation % 360:
            raster = raster.rotate(
                -rotation,
                resample=Image.Resampling.BICUBIC,
                expand=True,
            )
        output = BytesIO()
        raster.save(output, format="PNG")
        return output.getvalue(), rotated_width, rotated_height

    icon_path = STATIC_DIR / "ventilation-icons" / filename
    try:
        root = fromstring(icon_path.read_bytes())
        view_box = [
            float(value)
            for value in re.split(r"[\s,]+", root.attrib["viewBox"].strip())
        ]
        if len(view_box) != 4 or view_box[2] <= 0 or view_box[3] <= 0:
            raise ValueError("Invalid SVG viewBox.")
        origin_x, origin_y, original_width, original_height = view_box
        padding_x = (rotated_width - base_width) / 2
        padding_y = (rotated_height - base_height) / 2
        center_x = base_width / 2
        center_y = base_height / 2
        scale_x = base_width / original_width
        scale_y = base_height / original_height
        group = fromstring(
            f'<g xmlns="http://www.w3.org/2000/svg" transform="translate({padding_x} {padding_y}) translate({center_x} {center_y}) rotate({rotation}) translate({-center_x} {-center_y}) scale({scale_x} {scale_y}) translate({-origin_x} {-origin_y})">'
            "</g>"
        )
        for child in list(root):
            root.remove(child)
            group.append(child)
        root.append(group)
        root.set("viewBox", f"0 0 {rotated_width} {rotated_height}")
        root.set("width", f"{rotated_width}px")
        root.set("height", f"{rotated_height}px")
        svg_document = fitz.open(
            stream=tostring(root, encoding="utf-8"),
            filetype="svg",
        )
        try:
            pixmap = svg_document[0].get_pixmap(
                matrix=fitz.Matrix(3, 3),
                alpha=True,
            )
            return pixmap.tobytes("png"), rotated_width, rotated_height
        finally:
            svg_document.close()
    except ApiError:
        raise
    except (OSError, KeyError, ParseError, ValueError, fitz.FileDataError, RuntimeError) as exc:
        raise ApiError(422, "A ventilation product icon could not be rendered.") from exc


def _decode_ventilation_icon_images(icon_images):
    if icon_images is None:
        return None
    if (
        not isinstance(icon_images, dict)
        or len(icon_images) > len(VENT_ANNOTATION_ICON_FILES)
        or any(key not in VENT_ANNOTATION_ICON_FILES for key in icon_images)
    ):
        raise ApiError(400, "Ventilation icon images are invalid.")
    decoded = {}
    for key, data_url in icon_images.items():
        if not isinstance(data_url, str) or len(data_url) > 1_400_000:
            raise ApiError(400, "A ventilation icon image is invalid or too large.")
        match = re.fullmatch(r"data:image/png;base64,([A-Za-z0-9+/]+={0,2})", data_url)
        if not match:
            raise ApiError(400, "A ventilation icon image is invalid.")
        try:
            image_bytes = base64.b64decode(match.group(1), validate=True)
            if not image_bytes or len(image_bytes) > 1_000_000:
                raise ValueError("Invalid image size.")
            image = Image.open(BytesIO(image_bytes))
            if (
                image.format != "PNG"
                or image.width < 1
                or image.height < 1
                or image.width > 2048
                or image.height > 2048
                or image.width * image.height > 4_194_304
            ):
                raise ValueError("Invalid image dimensions.")
            image.load()
            decoded[key] = image.convert("RGBA")
        except (binascii.Error, OSError, ValueError) as exc:
            raise ApiError(400, "A ventilation icon image is invalid.") from exc
    return decoded


def _pdf_annotation_metadata_json(annotations):
    stored_annotations = []
    for annotation in annotations:
        tool = annotation["tool"]
        keys = {
            "pen": ("page", "tool", "color", "width", "points"),
            "highlighter": ("page", "tool", "color", "width", "points"),
            "text": (
                "page", "tool", "color", "point", "text", "font_size", "wrap",
                "text_width",
            ),
            "ventilation_icon": (
                "page", "tool", "point", "icon", "width", "height", "rotation",
            ),
        }[tool]
        stored_annotations.append(
            {key: annotation[key] for key in keys if key in annotation}
        )
    return json.dumps(stored_annotations, separators=(",", ":"), allow_nan=False)


def _annotate_pdf_document(
    content, annotations, allow_ventilation_icons=False, icon_images=None
):
    if not isinstance(annotations, list) or not annotations:
        raise ApiError(400, "Add at least one mark, product icon, or text note before saving.")
    if len(annotations) > HOUSE_DOCUMENT_MAX_ANNOTATIONS:
        raise ApiError(400, "This PDF has too many annotations to save at once.")
    browser_icon_images = _decode_ventilation_icon_images(icon_images)
    document = _open_pdf_document(content)
    try:
        for annotation in annotations:
            if not isinstance(annotation, dict):
                raise ApiError(400, "PDF annotations are invalid.")
            page_number = annotation.get("page")
            tool = annotation.get("tool")
            color_value = annotation.get("color")
            if (
                isinstance(page_number, bool)
                or not isinstance(page_number, int)
                or not 1 <= page_number <= document.page_count
                or tool not in {"pen", "highlighter", "text", "ventilation_icon"}
                or (
                    tool != "ventilation_icon"
                    and (
                        not isinstance(color_value, str)
                        or not re.fullmatch(r"#[0-9a-fA-F]{6}", color_value)
                    )
                )
            ):
                raise ApiError(400, "PDF annotations contain an invalid page, tool, or color.")
            page = document.load_page(page_number - 1)
            rect = page.rect
            if tool == "ventilation_icon":
                if not allow_ventilation_icons:
                    raise ApiError(
                        400, "Ventilation product icons can only be added to Vent or SEAI PDFs."
                    )
                icon_key = annotation.get("icon")
                point = annotation.get("point")
                width_ratio = annotation.get("width")
                height_ratio = annotation.get("height")
                rotation = annotation.get("rotation")
                if (
                    not isinstance(icon_key, str)
                    or icon_key not in VENT_ANNOTATION_ICON_FILES
                    or not isinstance(point, list)
                    or len(point) != 2
                    or not all(
                        isinstance(value, (int, float))
                        and not isinstance(value, bool)
                        and math.isfinite(value)
                        and 0 <= value <= 1
                        for value in point
                    )
                    or not isinstance(width_ratio, (int, float))
                    or isinstance(width_ratio, bool)
                    or not math.isfinite(width_ratio)
                    or not 0.02 <= width_ratio <= 0.7
                    or not isinstance(height_ratio, (int, float))
                    or isinstance(height_ratio, bool)
                    or not math.isfinite(height_ratio)
                    or not 0.02 <= height_ratio <= 0.7
                    or not isinstance(rotation, (int, float))
                    or isinstance(rotation, bool)
                    or not math.isfinite(rotation)
                    or not -3600 <= rotation <= 3600
                ):
                    raise ApiError(400, "A ventilation icon annotation is invalid.")
                if (
                    browser_icon_images is not None
                    and icon_key not in browser_icon_images
                ):
                    raise ApiError(400, "A ventilation icon image is missing.")
                base_width = rect.width * width_ratio
                base_height = rect.height * height_ratio
                image, image_width, image_height = _render_ventilation_icon(
                    icon_key,
                    rotation % 360,
                    base_width,
                    base_height,
                    browser_image=(
                        browser_icon_images.get(icon_key)
                        if browser_icon_images is not None
                        else None
                    ),
                )
                center_x = point[0] * rect.width
                center_y = point[1] * rect.height
                page.insert_image(
                    fitz.Rect(
                        center_x - image_width / 2,
                        center_y - image_height / 2,
                        center_x + image_width / 2,
                        center_y + image_height / 2,
                    ),
                    stream=image,
                    keep_proportion=False,
                    overlay=True,
                )
                continue
            color = tuple(
                int(color_value[index:index + 2], 16) / 255
                for index in (1, 3, 5)
            )
            if tool == "text":
                text = annotation.get("text")
                point = annotation.get("point")
                font_ratio = annotation.get("font_size")
                wrap_text = annotation.get("wrap", False)
                text_width_ratio = annotation.get("text_width", 0.92)
                if (
                    not isinstance(text, str)
                    or not text.strip()
                    or len(text) > 240
                    or not isinstance(point, list)
                    or len(point) != 2
                    or not isinstance(font_ratio, (int, float))
                    or not 0.005 <= font_ratio <= 0.1
                    or not isinstance(wrap_text, bool)
                    or not isinstance(text_width_ratio, (int, float))
                    or isinstance(text_width_ratio, bool)
                    or not 0.1 <= text_width_ratio <= 0.95
                    or not all(
                        isinstance(value, (int, float)) and 0 <= value <= 1
                        for value in point
                    )
                ):
                    raise ApiError(400, "A text annotation is invalid.")
                font_size = rect.width * font_ratio
                if wrap_text:
                    font = fitz.Font("helv")
                    max_width = max(24, rect.width * text_width_ratio)
                    x = min(point[0] * rect.width, rect.width - max_width - 8)
                    available_height = rect.height - 16
                    lines = _wrap_pdf_annotation_text(text, font, font_size, max_width)
                    for _ in range(8):
                        required_height = font_size * 1.6 * len(lines)
                        if required_height <= available_height:
                            break
                        font_size *= available_height / required_height
                        lines = _wrap_pdf_annotation_text(
                            text, font, font_size, max_width
                        )
                    required_height = font_size * 1.6 * len(lines)
                    if required_height > available_height:
                        raise ApiError(422, "The text does not fit on the blank PDF page.")
                    y = min(point[1] * rect.height, rect.height - 8 - required_height)
                    page.insert_textbox(
                        fitz.Rect(
                            x,
                            y,
                            min(rect.width - 8, x + max_width),
                            min(rect.height - 8, y + required_height + 2),
                        ),
                        "\n".join(lines),
                        fontsize=font_size,
                        fontname="helv",
                        color=color,
                        overlay=True,
                        lineheight=1.25,
                    )
                    continue
                page.insert_text(
                    (
                        point[0] * rect.width,
                        min(rect.height, point[1] * rect.height + font_size),
                    ),
                    text,
                    fontsize=font_size,
                    fontname="helv",
                    color=color,
                    overlay=True,
                )
                continue
            points = annotation.get("points")
            width_ratio = annotation.get("width")
            if (
                not isinstance(points, list)
                or not points
                or len(points) > HOUSE_DOCUMENT_MAX_ANNOTATION_POINTS
                or not isinstance(width_ratio, (int, float))
                or not 0.0005 <= width_ratio <= 0.05
            ):
                raise ApiError(400, "A pen annotation is invalid.")
            normalized_points = []
            for point in points:
                if (
                    not isinstance(point, list)
                    or len(point) != 2
                    or not all(
                        isinstance(value, (int, float)) and 0 <= value <= 1
                        for value in point
                    )
                ):
                    raise ApiError(400, "A pen annotation is outside the PDF page.")
                normalized_points.append(
                    fitz.Point(point[0] * rect.width, point[1] * rect.height)
                )
            width = max(0.5, rect.width * width_ratio)
            if len(normalized_points) == 1:
                point = normalized_points[0]
                page.draw_circle(
                    point,
                    width / 2,
                    color=color,
                    fill=color,
                    width=0,
                    overlay=True,
                )
            else:
                page.draw_polyline(
                    normalized_points,
                    color=color,
                    width=width,
                    lineCap=1,
                    lineJoin=1,
                    stroke_opacity=0.38 if tool == "highlighter" else 1,
                    overlay=True,
                )
        return document.tobytes(garbage=4, deflate=True)
    except ApiError:
        raise
    except (fitz.FileDataError, ValueError, RuntimeError) as exc:
        raise ApiError(422, "The annotations could not be added to this PDF.") from exc
    finally:
        document.close()


def _wrap_pdf_annotation_text(text, font, font_size, max_width):
    lines = []
    for paragraph in text.splitlines() or [text]:
        words = paragraph.split()
        if not words:
            lines.append("")
            continue
        current = ""
        for word in words:
            candidate = f"{current} {word}".strip()
            if font.text_length(candidate, fontsize=font_size) <= max_width:
                current = candidate
                continue
            if current:
                lines.append(current)
            current = ""
            for character in word:
                candidate = current + character
                if current and font.text_length(candidate, fontsize=font_size) > max_width:
                    lines.append(current)
                    current = character
                else:
                    current = candidate
        if current:
            lines.append(current)
    return lines


def _create_house_document_share_pdf(content, filename, house_id, survey_note):
    try:
        original = PdfReader(BytesIO(content), strict=False)
    except (PdfReadError, ValueError, OSError) as exc:
        raise ApiError(422, "This uploaded file could not be read as a PDF.") from exc
    if original.is_encrypted:
        raise ApiError(422, "Password-protected PDFs cannot be prepared for sharing.")
    if not original.pages:
        raise ApiError(422, "This PDF does not contain any pages.")
    if len(original.pages) > HOUSE_DOCUMENT_MAX_PAGES:
        raise ApiError(
            422,
            f"PDFs with more than {HOUSE_DOCUMENT_MAX_PAGES} pages cannot be prepared for sharing.",
        )
    if not survey_note:
        return bytes(content)

    font_path = Path(reportlab.__file__).parent / "fonts" / "Vera.ttf"
    font_name = "TrackerSurveyNotes"
    if font_name not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont(font_name, str(font_path)))
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "SurveyNoteTitle",
        parent=styles["Title"],
        fontName="Times-Bold",
        fontSize=20,
        leading=24,
        textColor=colors.HexColor("#233247"),
        alignment=TA_LEFT,
        spaceAfter=6 * mm,
    )
    details_style = ParagraphStyle(
        "SurveyNoteDetails",
        parent=styles["BodyText"],
        fontName=font_name,
        fontSize=9,
        leading=13,
        textColor=colors.HexColor("#303942"),
    )
    details_label_style = ParagraphStyle(
        "SurveyNoteDetailsLabel",
        parent=details_style,
        fontSize=7,
        leading=10,
        textColor=colors.HexColor("#68727c"),
        tracking=0.6,
    )
    note_style = ParagraphStyle(
        "SurveyNoteBody",
        parent=styles["BodyText"],
        fontName=font_name,
        fontSize=10.5,
        leading=16,
        textColor=colors.HexColor("#303942"),
        leftIndent=13,
        bulletIndent=0,
        bulletFontName=font_name,
        bulletFontSize=10,
        bulletColor=colors.HexColor("#34465a"),
    )
    note_lines = [line.strip() for line in survey_note.splitlines() if line.strip()]
    if not note_lines:
        note_lines = [survey_note.strip()]
    note_pdf = BytesIO()
    note_document = SimpleDocTemplate(
        note_pdf,
        pagesize=A4,
        rightMargin=22 * mm,
        leftMargin=22 * mm,
        topMargin=30 * mm,
        bottomMargin=24 * mm,
        title=f"Survey note - {house_id}",
    )

    def draw_page_chrome(pdf_canvas, document):
        page_width, page_height = A4
        pdf_canvas.saveState()
        pdf_canvas.setFillColor(colors.HexColor("#34465a"))
        pdf_canvas.setFont(font_name, 8)
        pdf_canvas.drawString(22 * mm, page_height - 17 * mm, "BRUSNA  |  MATERIAL TRACKER")
        pdf_canvas.setStrokeColor(colors.HexColor("#9aa5b1"))
        pdf_canvas.setLineWidth(0.7)
        pdf_canvas.line(22 * mm, page_height - 22 * mm, page_width - 22 * mm, page_height - 22 * mm)
        pdf_canvas.line(22 * mm, 17 * mm, page_width - 22 * mm, 17 * mm)
        pdf_canvas.setFillColor(colors.HexColor("#5f6973"))
        pdf_canvas.setFont(font_name, 7)
        pdf_canvas.drawString(22 * mm, 12 * mm, f"SURVEY NOTE  |  {house_id}")
        pdf_canvas.drawRightString(page_width - 22 * mm, 12 * mm, f"PAGE {document.page}")
        pdf_canvas.restoreState()

    metadata_table = Table(
        [
            [
                Paragraph("HOUSE", details_label_style),
                Paragraph(escape(house_id), details_style),
            ],
            [
                Paragraph("SOURCE PDF", details_label_style),
                Paragraph(escape(filename), details_style),
            ],
        ],
        colWidths=[34 * mm, 128 * mm],
        hAlign="LEFT",
    )
    metadata_table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), colors.white),
                ("LINEBELOW", (0, 0), (-1, -1), 0.5, colors.HexColor("#d5dbe1")),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ]
        )
    )
    note_document.build(
        [
            Paragraph("House survey note", title_style),
            metadata_table,
            Spacer(1, 9 * mm),
            Paragraph("SURVEY OBSERVATIONS", details_label_style),
            Spacer(1, 3 * mm),
            *[
                Paragraph(escape(line), note_style, bulletText="•")
                for line in note_lines
            ],
        ],
        onFirstPage=draw_page_chrome,
        onLaterPages=draw_page_chrome,
    )

    writer = PdfWriter()
    for page in original.pages:
        writer.add_page(page)
    note_reader = PdfReader(note_pdf)
    for page in note_reader.pages:
        writer.add_page(page)
    writer.add_metadata(
        {
            "/Title": f"{house_id} - {Path(filename).stem} with survey note",
            "/Subject": "Original survey PDF with the survey note appended on a separate page",
        }
    )
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def _create_seai_document_share_pdf(selections, documents, house_id, survey_note):
    writer = PdfWriter()
    filenames = set()
    for selection in selections:
        if selection.get("blank_page"):
            blank_pdf = _create_blank_annotated_pdf_page(
                selection.get("annotations")
            )
            reader = PdfReader(BytesIO(blank_pdf), strict=False)
            writer.add_page(reader.pages[0])
            continue
        content = selection.get("pdf_content")
        filename = selection.get("filename", "")
        if content is None:
            source = documents[selection["document_id"]]
            content = source["content"]
            filename = source["filename"]
        try:
            reader = PdfReader(BytesIO(content), strict=False)
        except (PdfReadError, ValueError, OSError) as exc:
            raise ApiError(422, "A selected SEAI PDF could not be read.") from exc
        if reader.is_encrypted:
            raise ApiError(422, "Password-protected SEAI PDFs cannot be shared.")
        page_number = selection["page"]
        if page_number < 1 or page_number > len(reader.pages):
            raise ApiError(
                400,
                f"Page {page_number} does not exist in {filename}.",
            )
        writer.add_page(reader.pages[page_number - 1])
        filenames.add(filename)
    selected_pages = BytesIO()
    writer.write(selected_pages)
    if not survey_note:
        return selected_pages.getvalue()
    source_label = (
        f"Selected SEAI pages ({len(selections)} pages from {len(filenames)} PDFs)"
    )
    return _create_house_document_share_pdf(
        selected_pages.getvalue(), source_label, house_id, survey_note
    )


def _create_blank_annotated_pdf_page(annotations):
    document = fitz.open()
    try:
        document.new_page(width=595, height=842)
        return _annotate_pdf_document(
            document.tobytes(), annotations
        )
    finally:
        document.close()


def _is_annotated_pdf_filename(filename):
    return bool(
        re.search(r"(?:-annotated)+(?:-\d+)?\.pdf$", str(filename), re.IGNORECASE)
    )


WORK_PLAN_TRADES = ("scaffolding", "esb", "plumber", "vent")
WORK_PLAN_SURVEY_QUESTIONS = (
    "ceramic_fuse_board",
    "solar_pv_required",
    "pir_required",
    "order_variations",
    "windows_required",
)


def _is_not_started_house(db, house_id):
    row = db.execute(
        """
        SELECT p.ready_for_start, h.id AS active_id
        FROM provided_houses p
        LEFT JOIN houses h ON h.id = p.id
        WHERE p.id = ?
        """,
        (house_id,),
    ).fetchone()
    return bool(row and not row["ready_for_start"] and row["active_id"] is None)


def _require_pdf_editable(db, house_id):
    if _is_not_started_house(db, house_id):
        raise ApiError(409, "PDFs are view-only while the house is Not started.")


def _clear_house_survey_records(db, house_id):
    db.execute("DELETE FROM house_surveys WHERE house_id = ?", (house_id,))
    db.execute("DELETE FROM house_work_plan_drafts WHERE house_id = ?", (house_id,))
    db.execute(
        "DELETE FROM house_insulation_assignments WHERE house_id = ?", (house_id,)
    )
    db.execute("DELETE FROM house_snags WHERE house_id = ?", (house_id,))
    db.execute("DELETE FROM house_checklists WHERE house_id = ?", (house_id,))
    db.execute("DELETE FROM house_vent_documents WHERE house_id = ?", (house_id,))
    db.execute("DELETE FROM seai_document_shares WHERE house_id = ?", (house_id,))
    db.execute("DELETE FROM seai_shared_pdf_deliveries WHERE house_id = ?", (house_id,))
    db.execute(
        "DELETE FROM seai_measure_variation_history WHERE house_id = ?", (house_id,)
    )
    db.execute(
        """
        UPDATE provided_houses
        SET notes = '',
            plan_scaffolding = NULL,
            plan_esb = NULL,
            plan_plumber = NULL,
            plan_scaffolding_required = NULL,
            plan_scaffolding_notes = '',
            plan_esb_required = NULL,
            plan_esb_notes = '',
            plan_plumber_required = NULL,
            plan_plumber_notes = '',
            plan_scaffolding_done_by = '',
            plan_scaffolding_done_at = '',
            plan_scaffolding_done_by_user_id = NULL,
            plan_esb_done_by = '',
            plan_esb_done_at = '',
            plan_esb_done_by_user_id = NULL,
            plan_plumber_done_by = '',
            plan_plumber_done_at = '',
            plan_plumber_done_by_user_id = NULL,
            plan_vent_required = NULL,
            plan_vent_notes = '',
            plan_vent_done_by = '',
            plan_vent_done_at = '',
            plan_vent_done_by_user_id = NULL,
            plan_ceramic_fuse_board = NULL,
            plan_solar_pv_required = NULL,
            plan_pir_required = NULL,
            plan_order_variations = NULL,
            plan_windows_required = NULL,
            plan_ceramic_fuse_board_done_by = '',
            plan_ceramic_fuse_board_done_at = '',
            plan_ceramic_fuse_board_done_by_user_id = NULL,
            plan_solar_pv_required_done_by = '',
            plan_solar_pv_required_done_at = '',
            plan_solar_pv_required_done_by_user_id = NULL,
            plan_pir_required_done_by = '',
            plan_pir_required_done_at = '',
            plan_pir_required_done_by_user_id = NULL,
            plan_order_variations_done_by = '',
            plan_order_variations_done_at = '',
            plan_order_variations_done_by_user_id = NULL,
            plan_windows_required_done_by = '',
            plan_windows_required_done_at = '',
            plan_windows_required_done_by_user_id = NULL
        WHERE id = ?
        """,
        (house_id,),
    )


    house_document_ids = [
        row["id"]
        for row in db.execute(
            "SELECT id, filename FROM house_documents WHERE house_id = ?",
            (house_id,),
        )
        if _is_annotated_pdf_filename(row["filename"])
    ]
    if house_document_ids:
        db.executemany(
            "DELETE FROM house_documents WHERE id = ?",
            ((document_id,) for document_id in house_document_ids),
        )

    seai_documents_table = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'seai_documents'"
    ).fetchone()
    if seai_documents_table:
        annotated_ids = [
            row["id"]
            for row in db.execute(
                """
                SELECT id, filename FROM seai_documents
                WHERE application_id = ?
                """,
                (house_id,),
            )
            if _is_annotated_pdf_filename(row["filename"])
        ]
        if db.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' "
            "AND name = 'selected_seai_documents'"
        ).fetchone():
            annotated_ids.extend(
                row["document_id"]
                for row in db.execute(
                    "SELECT document_id FROM selected_seai_documents WHERE house_id = ?",
                    (house_id,),
                )
                if row["document_id"] not in annotated_ids
            )
        if annotated_ids:
            db.executemany(
                "DELETE FROM seai_documents WHERE id = ?",
                ((document_id,) for document_id in annotated_ids),
            )
        if db.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' "
            "AND name = 'selected_seai_documents'"
        ).fetchone():
            db.execute(
                "DELETE FROM selected_seai_documents WHERE house_id = ?",
                (house_id,),
            )


def _record_house_survey_revision(db, house_id, survey, user, changed_at):
    existing = db.execute(
        "SELECT data, notes FROM house_surveys WHERE house_id = ?",
        (house_id,),
    ).fetchone()
    if existing is None:
        return
    try:
        previous_survey = json.loads(existing["data"])
    except (TypeError, ValueError):
        raise ApiError(500, "Previously saved survey data is invalid.")
    if not isinstance(previous_survey, dict):
        raise ApiError(500, "Previously saved survey data is invalid.")
    previous_survey.setdefault("notes", existing["notes"])
    if previous_survey == survey:
        return
    db.execute(
        """
        INSERT INTO house_survey_history
            (house_id, before_data, after_data, changed_by,
             changed_by_username, changed_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            house_id,
            json.dumps(previous_survey, ensure_ascii=False),
            json.dumps(survey, ensure_ascii=False),
            user["display_name"],
            user["username"],
            changed_at,
        ),
    )


@contextmanager
def connect_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def initialize_db():
    with connect_db() as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS materials (
                id INTEGER PRIMARY KEY,
                code TEXT,
                name TEXT NOT NULL COLLATE NOCASE UNIQUE,
                unit TEXT NOT NULL DEFAULT 'units',
                opening_qty REAL
            );
            CREATE TABLE IF NOT EXISTS houses (
                id TEXT PRIMARY KEY,
                postcode TEXT NOT NULL DEFAULT '',
                owner_name TEXT NOT NULL DEFAULT '',
                employee_name TEXT NOT NULL DEFAULT '',
                delivery_person TEXT NOT NULL DEFAULT '',
                delivery_team TEXT NOT NULL DEFAULT '',
                is_finished INTEGER NOT NULL DEFAULT 0,
                status_changed_at REAL NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS house_status_history (
                id INTEGER PRIMARY KEY,
                house_id TEXT NOT NULL,
                status TEXT NOT NULL,
                changed_by TEXT NOT NULL DEFAULT '',
                changed_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS house_status_history_house_idx
                ON house_status_history(house_id, id);
            CREATE TABLE IF NOT EXISTS house_checklists (
                house_id TEXT PRIMARY KEY REFERENCES provided_houses(id) ON DELETE CASCADE,
                scaffolding_status TEXT,
                esb_unclipped TEXT,
                plumber_status TEXT
            );
            CREATE TABLE IF NOT EXISTS movements (
                id INTEGER PRIMARY KEY,
                happened_on TEXT NOT NULL,
                kind TEXT NOT NULL,
                material_id INTEGER NOT NULL REFERENCES materials(id),
                qty REAL NOT NULL CHECK (qty > 0),
                source TEXT NOT NULL,
                destination TEXT NOT NULL,
                house_id TEXT REFERENCES houses(id),
                delivery_number INTEGER,
                person TEXT NOT NULL DEFAULT '',
                team TEXT NOT NULL DEFAULT '',
                department TEXT NOT NULL DEFAULT '',
                notes TEXT NOT NULL DEFAULT '',
                recorded_at TEXT NOT NULL DEFAULT '',
                FOREIGN KEY (material_id) REFERENCES materials(id)
            );
            CREATE INDEX IF NOT EXISTS movements_material_idx ON movements(material_id, id);
            CREATE TABLE IF NOT EXISTS stocktakes (
                id INTEGER PRIMARY KEY,
                material_id INTEGER NOT NULL REFERENCES materials(id),
                location TEXT NOT NULL,
                quantity REAL NOT NULL CHECK (quantity >= 0),
                movement_id INTEGER NOT NULL,
                counted_on TEXT NOT NULL,
                counted_by TEXT NOT NULL DEFAULT '',
                notes TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS stocktakes_location_idx
                ON stocktakes(material_id, location, id);
            CREATE TABLE IF NOT EXISTS app_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS app_roles (
                role_key TEXT PRIMARY KEY,
                display_name TEXT NOT NULL UNIQUE,
                is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1))
            );
            CREATE TABLE IF NOT EXISTS app_permissions (
                permission_key TEXT PRIMARY KEY,
                description TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS app_role_permissions (
                role_key TEXT NOT NULL REFERENCES app_roles(role_key) ON DELETE CASCADE,
                permission_key TEXT NOT NULL REFERENCES app_permissions(permission_key) ON DELETE CASCADE,
                is_allowed INTEGER NOT NULL DEFAULT 1 CHECK (is_allowed IN (0, 1)),
                PRIMARY KEY (role_key, permission_key)
            );
            CREATE TABLE IF NOT EXISTS app_users (
                id INTEGER PRIMARY KEY,
                username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                display_name TEXT NOT NULL,
                role TEXT NOT NULL,
                password_salt BLOB NOT NULL,
                password_hash BLOB NOT NULL,
                password_iterations INTEGER NOT NULL,
                credential_type TEXT NOT NULL DEFAULT 'pin',
                is_active INTEGER NOT NULL DEFAULT 1,
                supervisor_specialty TEXT NOT NULL DEFAULT '',
                team_name TEXT NOT NULL DEFAULT '',
                work_phone TEXT,
                home_phone TEXT,
                company_email TEXT,
                private_email TEXT
            );
            CREATE TABLE IF NOT EXISTS departments (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL UNIQUE COLLATE NOCASE,
                is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1))
            );
            CREATE TABLE IF NOT EXISTS user_departments (
                user_id INTEGER NOT NULL REFERENCES app_users(id) ON DELETE CASCADE,
                department_id INTEGER NOT NULL REFERENCES departments(id) ON DELETE RESTRICT,
                is_primary INTEGER NOT NULL DEFAULT 0 CHECK (is_primary IN (0, 1)),
                PRIMARY KEY (user_id, department_id)
            );
            CREATE TABLE IF NOT EXISTS user_reporting_lines (
                manager_user_id INTEGER NOT NULL REFERENCES app_users(id) ON DELETE CASCADE,
                report_user_id INTEGER NOT NULL REFERENCES app_users(id) ON DELETE CASCADE,
                relationship TEXT NOT NULL DEFAULT 'manager',
                PRIMARY KEY (manager_user_id, report_user_id),
                CHECK (manager_user_id != report_user_id)
            );
            CREATE INDEX IF NOT EXISTS user_reporting_report_idx
                ON user_reporting_lines(report_user_id);
            CREATE TABLE IF NOT EXISTS team_roster_members (
                id INTEGER PRIMARY KEY,
                teamlead_user_id INTEGER NOT NULL
                    REFERENCES app_users(id) ON DELETE CASCADE,
                member_name TEXT NOT NULL COLLATE NOCASE
                    CHECK (length(trim(member_name)) BETWEEN 1 AND 120),
                UNIQUE (teamlead_user_id, member_name)
            );
            CREATE TABLE IF NOT EXISTS app_sessions (
                token_hash TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES app_users(id) ON DELETE CASCADE,
                expires_at INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS app_sessions_expiry_idx
                ON app_sessions(expires_at);
            CREATE TABLE IF NOT EXISTS app_login_attempts (
                username_hash TEXT PRIMARY KEY,
                failures INTEGER NOT NULL,
                window_started_at INTEGER NOT NULL,
                locked_until INTEGER NOT NULL DEFAULT 0
            );
            """
        )
        db.executemany(
            "INSERT OR IGNORE INTO app_roles(role_key, display_name) VALUES (?, ?)",
            (
                ("admin", "Admin"),
                ("assign", "Assign"),
                ("boss", "Boss"),
                ("surveyor", "Surveyor"),
                ("warehouse", "Warehouse"),
                ("delivery", "Delivery"),
                ("supervisor", "Supervisor"),
                ("teamlead", "Teamlead"),
                ("plumber", "Plumber"),
                ("plumbing_coordinator", "Plumbing Coordinator"),
                ("ventilation_coordinator", "Vent Coordinator"),
                ("employee", "Employee"),
            ),
        )
        db.execute(
            """
            INSERT OR IGNORE INTO app_permissions(permission_key, description)
            VALUES ('*', 'All application features and operations')
            """
        )
        db.execute(
            """
            INSERT OR IGNORE INTO app_role_permissions
                (role_key, permission_key, is_allowed)
            VALUES ('admin', '*', 1)
            """
        )
        access_profiles = (
            ("access.admin", "Administrator account management and unrestricted access."),
            ("access.assign", "House assignment and workflow controls without materials access."),
            ("access.boss", "Boss dashboard and operational views."),
            ("access.surveyor", "Surveyor room, ready-house surveys, and survey documents."),
            ("access.warehouse", "Warehouse inventory and material movement features."),
            ("access.delivery", "Delivery-related views and operations."),
            ("access.supervisor", "Assigned-house supervisor dashboard and team-lead oversight."),
            ("access.teamlead", "Team-lead dashboard and own team roster management."),
            ("access.plumber", "Employee-style operational access for plumbing work."),
            ("access.plumbing_coordinator", "Plumbing work queue and assignment to Plumbing supervisors."),
            ("access.ventilation_coordinator", "Ventilation work queue and assignment to Ventilation teams."),
            ("access.vent_photos", "View Vent team photos attached to houses."),
            ("access.employee", "Standard employee dashboard and material movement access."),
        )
        db.executemany(
            "INSERT OR IGNORE INTO app_permissions(permission_key, description) VALUES (?, ?)",
            access_profiles,
        )
        for role_key, _display_name in (
            ("assign", "Assign"),
            ("boss", "Boss"),
            ("surveyor", "Surveyor"),
            ("warehouse", "Warehouse"),
            ("delivery", "Delivery"),
            ("supervisor", "Supervisor"),
            ("teamlead", "Teamlead"),
            ("plumber", "Plumber"),
            ("plumbing_coordinator", "Plumbing Coordinator"),
            ("ventilation_coordinator", "Vent Coordinator"),
            ("employee", "Employee"),
        ):
            db.execute(
                """
                INSERT OR IGNORE INTO app_role_permissions
                    (role_key, permission_key, is_allowed)
                VALUES (?, ?, 1)
                """,
                (role_key, f"access.{role_key}"),
            )
        for department_name in (
            "Administration",
            "External Wall Insulation",
            "Surveyors",
            "Delivery",
            "Ventilation",
            "Attic",
            "Plumbing",
        ):
            db.execute(
                "INSERT OR IGNORE INTO departments(name) VALUES (?)",
                (department_name,),
            )
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS user_departments (
                user_id INTEGER NOT NULL REFERENCES app_users(id) ON DELETE CASCADE,
                department_id INTEGER NOT NULL REFERENCES departments(id) ON DELETE RESTRICT,
                is_primary INTEGER NOT NULL DEFAULT 0 CHECK (is_primary IN (0, 1)),
                PRIMARY KEY (user_id, department_id)
            )
            """
        )
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS user_reporting_lines (
                manager_user_id INTEGER NOT NULL REFERENCES app_users(id) ON DELETE CASCADE,
                report_user_id INTEGER NOT NULL REFERENCES app_users(id) ON DELETE CASCADE,
                relationship TEXT NOT NULL DEFAULT 'manager',
                PRIMARY KEY (manager_user_id, report_user_id),
                CHECK (manager_user_id != report_user_id)
            )
            """
        )
        db.execute(
            "CREATE INDEX IF NOT EXISTS user_reporting_report_idx "
            "ON user_reporting_lines(report_user_id)"
        )
        if db.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' "
            "AND name = 'seai_documents'"
        ).fetchone():
            seai_document_columns = {
                row["name"] for row in db.execute("PRAGMA table_info(seai_documents)")
            }
            if "content" not in seai_document_columns:
                db.execute("ALTER TABLE seai_documents ADD COLUMN content BLOB")
        status_history_foreign_keys = {
            row["table"]
            for row in db.execute("PRAGMA foreign_key_list(house_status_history)")
        }
        if "houses" in status_history_foreign_keys:
            db.execute(
                "ALTER TABLE house_status_history RENAME TO house_status_history_old"
            )
            db.execute(
                """
                CREATE TABLE house_status_history (
                    id INTEGER PRIMARY KEY,
                    house_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    changed_by TEXT NOT NULL DEFAULT '',
                    changed_at TEXT NOT NULL
                )
                """
            )
            db.execute(
                """
                INSERT INTO house_status_history(id, house_id, status, changed_by, changed_at)
                SELECT id, house_id, status, changed_by, changed_at
                FROM house_status_history_old
                """
            )
            db.execute("DROP TABLE house_status_history_old")
            db.execute(
                """
                CREATE INDEX house_status_history_house_idx
                ON house_status_history(house_id, id)
                """
            )
        house_columns = {
            row["name"] for row in db.execute("PRAGMA table_info(houses)")
        }
        if "is_archived" in house_columns and sqlite3.sqlite_version_info >= (3, 35, 0):
            db.execute("ALTER TABLE houses DROP COLUMN is_archived")
            house_columns.remove("is_archived")
        if "is_finished" not in house_columns:
            db.execute(
                "ALTER TABLE houses ADD COLUMN is_finished INTEGER NOT NULL DEFAULT 0"
            )
        if "status_changed_at" not in house_columns:
            db.execute(
                "ALTER TABLE houses ADD COLUMN status_changed_at REAL NOT NULL DEFAULT 0"
            )
        movement_columns = {
            row["name"] for row in db.execute("PRAGMA table_info(movements)")
        }
        if "house_id" not in movement_columns:
            db.execute("ALTER TABLE movements ADD COLUMN house_id TEXT REFERENCES houses(id)")
            db.execute(
                """
                UPDATE movements
                SET house_id = CASE
                    WHEN destination LIKE 'house:%' THEN substr(destination, 7)
                    WHEN source LIKE 'house:%' THEN substr(source, 7)
                    ELSE NULL
                END
                WHERE house_id IS NULL
                """
            )
        if "delivery_number" not in movement_columns:
            db.execute("ALTER TABLE movements ADD COLUMN delivery_number INTEGER")
        delivery_rows = db.execute(
            "SELECT id, house_id FROM movements "
            "WHERE kind = 'delivery' AND delivery_number IS NULL ORDER BY id"
        ).fetchall()
        delivery_numbers = {
            row["house_id"]: row["number"]
            for row in db.execute(
                """
                SELECT house_id, MAX(delivery_number) AS number
                FROM movements
                WHERE kind = 'delivery' AND delivery_number IS NOT NULL
                GROUP BY house_id
                """
            )
        }
        for row in delivery_rows:
            if not row["house_id"]:
                continue
            number = delivery_numbers.get(row["house_id"], 0) + 1
            delivery_numbers[row["house_id"]] = number
            db.execute(
                "UPDATE movements SET delivery_number = ? WHERE id = ?",
                (number, row["id"]),
            )
        house_columns = {row["name"] for row in db.execute("PRAGMA table_info(houses)")}
        if "employee_name" not in house_columns:
            db.execute(
                "ALTER TABLE houses ADD COLUMN employee_name TEXT NOT NULL DEFAULT ''"
            )
        if "delivery_person" not in house_columns:
            db.execute(
                "ALTER TABLE houses ADD COLUMN delivery_person TEXT NOT NULL DEFAULT ''"
            )
        if "delivery_team" not in house_columns:
            db.execute(
                "ALTER TABLE houses ADD COLUMN delivery_team TEXT NOT NULL DEFAULT ''"
            )
        if "stage" not in house_columns:
            db.execute("ALTER TABLE houses ADD COLUMN stage INTEGER NOT NULL DEFAULT 1")
        db.execute("UPDATE houses SET stage = 1 WHERE stage NOT IN (1, 2)")
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS provided_houses (
                id TEXT PRIMARY KEY,
                postcode TEXT NOT NULL DEFAULT '',
                address TEXT NOT NULL DEFAULT '',
                owner_name TEXT NOT NULL DEFAULT '',
                area_m2 REAL,
                perimeter_m REAL,
                source TEXT NOT NULL DEFAULT 'tracker'
            )
            """
        )
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS house_role_assignments (
                house_id TEXT NOT NULL REFERENCES provided_houses(id) ON DELETE CASCADE,
                role TEXT NOT NULL CHECK (role IN ('surveyor', 'supervisor')),
                user_id INTEGER NOT NULL REFERENCES app_users(id) ON DELETE CASCADE,
                assigned_by TEXT NOT NULL DEFAULT '',
                assigned_at TEXT NOT NULL,
                PRIMARY KEY (house_id, role)
            )
            """
        )
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS house_plumbing_assignments (
                house_id TEXT PRIMARY KEY REFERENCES provided_houses(id) ON DELETE CASCADE,
                supervisor_user_id INTEGER NOT NULL REFERENCES app_users(id) ON DELETE CASCADE,
                assigned_by TEXT NOT NULL DEFAULT '',
                assigned_at TEXT NOT NULL
            )
            """
        )
        db.execute(
            """
            CREATE INDEX IF NOT EXISTS house_plumbing_assignments_supervisor_idx
            ON house_plumbing_assignments(supervisor_user_id, house_id)
            """
        )
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS house_ewi_assignments (
                house_id TEXT PRIMARY KEY REFERENCES provided_houses(id) ON DELETE CASCADE,
                teamlead_user_id INTEGER NOT NULL REFERENCES app_users(id) ON DELETE CASCADE,
                supervisor_user_id INTEGER NOT NULL REFERENCES app_users(id) ON DELETE CASCADE,
                assigned_by TEXT NOT NULL DEFAULT '',
                assigned_at TEXT NOT NULL,
                completed_by TEXT NOT NULL DEFAULT '',
                completed_at TEXT NOT NULL DEFAULT ''
            )
            """
        )
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS house_insulation_assignments (
                house_id TEXT NOT NULL
                    REFERENCES provided_houses(id) ON DELETE CASCADE,
                work_type TEXT NOT NULL CHECK (work_type IN ('attic', 'cavity')),
                teamlead_user_id INTEGER NOT NULL
                    REFERENCES app_users(id) ON DELETE CASCADE,
                assigned_by TEXT NOT NULL DEFAULT '',
                assigned_at TEXT NOT NULL,
                completed_by TEXT NOT NULL DEFAULT '',
                completed_at TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (house_id, work_type)
            )
            """
        )
        db.execute(
            """
            CREATE INDEX IF NOT EXISTS house_insulation_assignments_teamlead_idx
            ON house_insulation_assignments(teamlead_user_id, work_type, house_id)
            """
        )
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS house_snags (
                id INTEGER PRIMARY KEY,
                house_id TEXT NOT NULL REFERENCES provided_houses(id) ON DELETE CASCADE,
                text TEXT NOT NULL,
                done INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL DEFAULT 0
            )
            """
        )
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS house_surveys (
                house_id TEXT PRIMARY KEY REFERENCES provided_houses(id) ON DELETE CASCADE,
                data TEXT NOT NULL,
                notes TEXT NOT NULL DEFAULT '',
                updated_by TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL
            )
            """
        )
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS house_survey_history (
                id INTEGER PRIMARY KEY,
                house_id TEXT NOT NULL REFERENCES provided_houses(id) ON DELETE CASCADE,
                before_data TEXT NOT NULL,
                after_data TEXT NOT NULL,
                changed_by TEXT NOT NULL DEFAULT '',
                changed_by_username TEXT NOT NULL DEFAULT '',
                changed_at TEXT NOT NULL
            )
            """
        )
        db.execute(
            "CREATE INDEX IF NOT EXISTS house_survey_history_house_idx "
            "ON house_survey_history(house_id, id)"
        )
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS seai_measure_variation_history (
                id INTEGER PRIMARY KEY,
                house_id TEXT NOT NULL REFERENCES provided_houses(id) ON DELETE CASCADE,
                measure_index INTEGER NOT NULL,
                measure_name TEXT NOT NULL DEFAULT '',
                survey_quantity TEXT NOT NULL DEFAULT '',
                previous_variation TEXT NOT NULL DEFAULT '',
                variation TEXT NOT NULL DEFAULT '',
                changed_by TEXT NOT NULL DEFAULT '',
                changed_by_username TEXT NOT NULL DEFAULT '',
                changed_at TEXT NOT NULL
            )
            """
        )
        db.execute(
            "CREATE INDEX IF NOT EXISTS seai_measure_variation_history_house_idx "
            "ON seai_measure_variation_history(house_id, id)"
        )
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS house_work_plan_drafts (
                house_id TEXT PRIMARY KEY REFERENCES provided_houses(id) ON DELETE CASCADE,
                data TEXT NOT NULL,
                saved_by TEXT NOT NULL DEFAULT '',
                saved_at TEXT NOT NULL
            )
            """
        )
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS house_documents (
                id INTEGER PRIMARY KEY,
                house_id TEXT NOT NULL REFERENCES provided_houses(id) ON DELETE CASCADE,
                filename TEXT NOT NULL,
                content BLOB NOT NULL,
                size_bytes INTEGER NOT NULL,
                uploaded_by TEXT NOT NULL DEFAULT '',
                uploaded_at TEXT NOT NULL
            )
            """
        )
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS house_vent_documents (
                id INTEGER PRIMARY KEY,
                house_id TEXT NOT NULL REFERENCES provided_houses(id) ON DELETE CASCADE,
                filename TEXT NOT NULL,
                content BLOB NOT NULL,
                size_bytes INTEGER NOT NULL,
                shared_by TEXT NOT NULL DEFAULT '',
                shared_at TEXT NOT NULL
            )
            """
        )
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS house_vent_photos (
                id INTEGER PRIMARY KEY,
                house_id TEXT NOT NULL REFERENCES provided_houses(id) ON DELETE CASCADE,
                filename TEXT NOT NULL,
                mime_type TEXT NOT NULL,
                content BLOB NOT NULL,
                size_bytes INTEGER NOT NULL,
                uploaded_by TEXT NOT NULL DEFAULT '',
                uploaded_at TEXT NOT NULL
            )
            """
        )
        db.execute(
            """
            CREATE INDEX IF NOT EXISTS house_vent_photos_house_idx
            ON house_vent_photos(house_id, id DESC)
            """
        )
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS house_plumbing_photos (
                id INTEGER PRIMARY KEY,
                house_id TEXT NOT NULL REFERENCES provided_houses(id) ON DELETE CASCADE,
                filename TEXT NOT NULL,
                mime_type TEXT NOT NULL,
                content BLOB NOT NULL,
                size_bytes INTEGER NOT NULL,
                uploaded_by TEXT NOT NULL DEFAULT '',
                uploaded_at TEXT NOT NULL,
                activity TEXT NOT NULL DEFAULT ''
            )
            """
        )
        plumbing_photo_columns = {
            row["name"]
            for row in db.execute("PRAGMA table_info(house_plumbing_photos)")
        }
        if "activity" not in plumbing_photo_columns:
            db.execute(
                """
                ALTER TABLE house_plumbing_photos
                ADD COLUMN activity TEXT NOT NULL DEFAULT ''
                """
            )
        db.execute(
            """
            CREATE INDEX IF NOT EXISTS house_plumbing_photos_house_idx
            ON house_plumbing_photos(house_id, id DESC)
            """
        )
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS app_user_plumbing_photo_access (
                user_id INTEGER PRIMARY KEY REFERENCES app_users(id) ON DELETE CASCADE,
                granted_by TEXT NOT NULL DEFAULT '',
                granted_at TEXT NOT NULL
            )
            """
        )
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS house_vent_document_annotations (
                annotated_document_id INTEGER PRIMARY KEY
                    REFERENCES house_vent_documents(id) ON DELETE CASCADE,
                source_document_id INTEGER
                    REFERENCES house_vent_documents(id) ON DELETE SET NULL,
                annotations_json TEXT NOT NULL,
                saved_at TEXT NOT NULL
            )
            """
        )
        annotation_columns = {
            row["name"]
            for row in db.execute(
                "PRAGMA table_info(house_vent_document_annotations)"
            )
        }
        if "source_seai_document_id" not in annotation_columns:
            db.execute(
                """
                ALTER TABLE house_vent_document_annotations
                ADD COLUMN source_seai_document_id INTEGER
                """
            )
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS house_vent_assignments (
                house_id TEXT PRIMARY KEY
                    REFERENCES provided_houses(id) ON DELETE CASCADE,
                teamlead_user_id INTEGER NOT NULL
                    REFERENCES app_users(id) ON DELETE RESTRICT,
                assigned_by TEXT NOT NULL DEFAULT '',
                assigned_at TEXT NOT NULL
            )
            """
        )
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS seai_document_shares (
                id INTEGER PRIMARY KEY,
                document_id INTEGER NOT NULL,
                house_id TEXT NOT NULL REFERENCES provided_houses(id) ON DELETE CASCADE,
                recipient_user_id INTEGER NOT NULL REFERENCES app_users(id) ON DELETE CASCADE,
                shared_by TEXT NOT NULL DEFAULT '',
                shared_at TEXT NOT NULL,
                UNIQUE(document_id, recipient_user_id)
            )
            """
        )
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS selected_seai_documents (
                document_id INTEGER PRIMARY KEY,
                house_id TEXT NOT NULL REFERENCES provided_houses(id) ON DELETE CASCADE,
                created_by TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            )
            """
        )
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS seai_shared_pdf_deliveries (
                id INTEGER PRIMARY KEY,
                house_id TEXT NOT NULL REFERENCES provided_houses(id) ON DELETE CASCADE,
                recipient_user_id INTEGER NOT NULL REFERENCES app_users(id) ON DELETE CASCADE,
                filename TEXT NOT NULL,
                content BLOB NOT NULL,
                size_bytes INTEGER NOT NULL,
                shared_by TEXT NOT NULL DEFAULT '',
                shared_at TEXT NOT NULL
            )
            """
        )
        provided_columns = {
            row["name"] for row in db.execute("PRAGMA table_info(provided_houses)")
        }
        for column, definition in (
            ("ready_for_start", "INTEGER NOT NULL DEFAULT 0"),
            ("notes", "TEXT NOT NULL DEFAULT ''"),
            ("plan_scaffolding", "TEXT"),
            ("plan_esb", "TEXT"),
            ("plan_plumber", "TEXT"),
            ("plan_scaffolding_required", "INTEGER"),
            ("plan_scaffolding_notes", "TEXT NOT NULL DEFAULT ''"),
            ("plan_esb_required", "INTEGER"),
            ("plan_esb_notes", "TEXT NOT NULL DEFAULT ''"),
            ("plan_plumber_required", "INTEGER"),
            ("plan_plumber_notes", "TEXT NOT NULL DEFAULT ''"),
            ("plan_vent_required", "INTEGER"),
            ("plan_vent_notes", "TEXT NOT NULL DEFAULT ''"),
            *(
                (f"plan_{question}", "INTEGER")
                for question in WORK_PLAN_SURVEY_QUESTIONS
            ),
            *(
                (f"plan_{item}_{field}", "TEXT NOT NULL DEFAULT ''")
                for item in (*WORK_PLAN_TRADES, *WORK_PLAN_SURVEY_QUESTIONS)
                for field in ("done_by", "done_at")
            ),
            *(
                (f"plan_{item}_done_by_user_id", "INTEGER")
                for item in (*WORK_PLAN_TRADES, *WORK_PLAN_SURVEY_QUESTIONS)
            ),
            ("latitude", "REAL"),
            ("longitude", "REAL"),
        ):
            if column not in provided_columns:
                db.execute(f"ALTER TABLE provided_houses ADD COLUMN {column} {definition}")
        for item in (*WORK_PLAN_TRADES, *WORK_PLAN_SURVEY_QUESTIONS):
            db.execute(
                f"""
                UPDATE provided_houses
                SET plan_{item}_done_by_user_id = (
                    SELECT users.id
                    FROM app_users users
                    WHERE users.display_name = provided_houses.plan_{item}_done_by
                        COLLATE NOCASE
                )
                WHERE plan_{item}_done_by_user_id IS NULL
                  AND plan_{item}_done_by != ''
                  AND (
                      SELECT COUNT(*)
                      FROM app_users users
                      WHERE users.display_name = provided_houses.plan_{item}_done_by
                          COLLATE NOCASE
                  ) = 1
                """
            )
        # Every house the tracker already works on belongs to the provided list.
        db.execute(
            """
            INSERT OR IGNORE INTO provided_houses(id, postcode, owner_name, source)
            SELECT id, postcode, owner_name, 'tracker' FROM houses
            """
        )
        checklist_parent = next(
            (
                row["table"]
                for row in db.execute("PRAGMA foreign_key_list(house_checklists)")
                if row["from"] == "house_id"
            ),
            None,
        )
        if checklist_parent != "provided_houses":
            db.execute("ALTER TABLE house_checklists RENAME TO house_checklists_old")
            db.execute(
                """
                CREATE TABLE house_checklists (
                    house_id TEXT PRIMARY KEY REFERENCES provided_houses(id) ON DELETE CASCADE,
                    scaffolding_status TEXT,
                    esb_unclipped TEXT,
                    plumber_status TEXT
                )
                """
            )
            db.execute(
                """
                INSERT INTO house_checklists
                    (house_id, scaffolding_status, esb_unclipped, plumber_status)
                SELECT house_id, scaffolding_status, esb_unclipped, plumber_status
                FROM house_checklists_old
                """
            )
            db.execute("DROP TABLE house_checklists_old")
        references_houses = any(
            row["table"] == "houses"
            for row in db.execute("PRAGMA foreign_key_list(material_requests)")
        )
        if references_houses:
            db.execute("ALTER TABLE material_requests RENAME TO material_requests_old")
            db.execute("DROP INDEX IF EXISTS material_requests_house_idx")
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS material_requests (
                id INTEGER PRIMARY KEY,
                house_id TEXT NOT NULL REFERENCES provided_houses(id),
                created_at TEXT NOT NULL,
                created_by TEXT NOT NULL DEFAULT '',
                notes TEXT NOT NULL DEFAULT '',
                data TEXT NOT NULL
            )
            """
        )
        if references_houses:
            db.execute(
                "INSERT INTO material_requests SELECT id, house_id, created_at, created_by, notes, data "
                "FROM material_requests_old"
            )
            db.execute("DROP TABLE material_requests_old")
        db.execute(
            "CREATE INDEX IF NOT EXISTS material_requests_house_idx ON material_requests(house_id, id)"
        )
        if "department" not in movement_columns:
            db.execute(
                "ALTER TABLE movements ADD COLUMN department TEXT NOT NULL DEFAULT ''"
            )
        if "recorded_at" not in movement_columns:
            db.execute(
                "ALTER TABLE movements ADD COLUMN recorded_at TEXT NOT NULL DEFAULT ''"
            )
        # Open houses must hold what was delivered: drop close-outs left behind by earlier reopen actions.
        db.execute(
            "DELETE FROM movements WHERE kind = 'use' AND notes = ? "
            "AND house_id IN (SELECT id FROM houses WHERE is_finished = 0)",
            (CLOSEOUT_NOTE,),
        )
        db.execute("CREATE INDEX IF NOT EXISTS movements_house_idx ON movements(house_id)")
        db.execute(
            "CREATE INDEX IF NOT EXISTS movements_delivery_idx ON movements(house_id, delivery_number)"
        )
        user_columns = {row["name"] for row in db.execute("PRAGMA table_info(app_users)")}
        if "credential_type" not in user_columns:
            db.execute(
                "ALTER TABLE app_users ADD COLUMN credential_type TEXT NOT NULL DEFAULT 'password'"
            )
        if "supervisor_specialty" not in user_columns:
            db.execute(
                "ALTER TABLE app_users ADD COLUMN supervisor_specialty TEXT NOT NULL DEFAULT ''"
            )
        if "team_name" not in user_columns:
            db.execute(
                "ALTER TABLE app_users ADD COLUMN team_name TEXT NOT NULL DEFAULT ''"
            )
        if "company_email" not in user_columns:
            if "work_email" in user_columns:
                db.execute(
                    "ALTER TABLE app_users RENAME COLUMN work_email TO company_email"
                )
            else:
                db.execute("ALTER TABLE app_users ADD COLUMN company_email TEXT")
        for column in ("work_phone", "home_phone", "private_email"):
            if column not in user_columns:
                db.execute(f"ALTER TABLE app_users ADD COLUMN {column} TEXT")
        db.execute(
            """
            INSERT OR IGNORE INTO house_plumbing_assignments
                (house_id, supervisor_user_id, assigned_by, assigned_at)
            SELECT assignment.house_id, assignment.user_id,
                   assignment.assigned_by, assignment.assigned_at
            FROM house_role_assignments assignment
            JOIN app_users users ON users.id = assignment.user_id
            WHERE assignment.role = 'supervisor'
              AND (
                  users.supervisor_specialty = 'Plumbing' COLLATE NOCASE
                  OR EXISTS (
                      SELECT 1 FROM user_departments memberships
                      JOIN departments ON departments.id = memberships.department_id
                      WHERE memberships.user_id = users.id
                        AND departments.name = 'Plumbing' COLLATE NOCASE
                        AND departments.is_active = 1
                  )
              )
            """
        )
    _migrate_user_roles()
    _seed_user_departments()


def _migrate_user_roles():
    with connect_db() as db:
        schema = db.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'app_users'"
        ).fetchone()[0]
        if not re.search(r"role\s+TEXT\s+NOT\s+NULL\s+CHECK", schema, re.IGNORECASE):
            return
        db.execute("PRAGMA foreign_keys = OFF")
        db.execute("BEGIN IMMEDIATE")
        try:
            db.execute("""
                CREATE TABLE app_users_new (
                    id INTEGER PRIMARY KEY,
                    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                    display_name TEXT NOT NULL,
                    role TEXT NOT NULL,
                    password_salt BLOB NOT NULL,
                    password_hash BLOB NOT NULL,
                    password_iterations INTEGER NOT NULL,
                    credential_type TEXT NOT NULL DEFAULT 'pin',
                    is_active INTEGER NOT NULL DEFAULT 1,
                    supervisor_specialty TEXT NOT NULL DEFAULT '',
                    team_name TEXT NOT NULL DEFAULT '',
                    work_phone TEXT,
                    home_phone TEXT,
                    company_email TEXT,
                    private_email TEXT
                )
            """)
            db.execute("INSERT INTO app_users_new SELECT id, username, display_name, role, password_salt, password_hash, password_iterations, credential_type, is_active, supervisor_specialty, team_name, work_phone, home_phone, company_email, private_email FROM app_users")
            db.execute("""CREATE TABLE app_sessions_new (
                token_hash TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES app_users_new(id) ON DELETE CASCADE,
                expires_at INTEGER NOT NULL
            )""")
            db.execute("INSERT INTO app_sessions_new SELECT token_hash, user_id, expires_at FROM app_sessions")
            db.execute("DROP TABLE app_sessions")
            db.execute("DROP TABLE app_users")
            db.execute("ALTER TABLE app_users_new RENAME TO app_users")
            db.execute("ALTER TABLE app_sessions_new RENAME TO app_sessions")
            db.execute("CREATE INDEX app_sessions_expiry_idx ON app_sessions(expires_at)")
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.execute("PRAGMA foreign_keys = ON")


def _seed_user_departments():
    with connect_db() as db:
        department_ids = {
            row["name"]: row["id"]
            for row in db.execute("SELECT id, name FROM departments")
        }
        for user in db.execute(
            "SELECT id, username, role, supervisor_specialty, team_name FROM app_users"
        ):
            if db.execute(
                "SELECT 1 FROM user_departments WHERE user_id = ? LIMIT 1",
                (user["id"],),
            ).fetchone():
                continue
            if user["role"] in {"admin", "assign"}:
                department = "Administration"
            elif user["role"] == "surveyor":
                department = "Surveyors"
            elif user["role"] == "delivery":
                department = "Delivery"
            elif user["role"] == "supervisor":
                specialty = user["supervisor_specialty"]
                department = specialty if specialty in department_ids else "Administration"
            elif user["role"] == "teamlead":
                department = user["team_name"] if user["team_name"] in department_ids else "External Wall Insulation"
            elif user["role"] == "plumber":
                department = "Plumbing"
            elif user["role"] == "plumbing_coordinator":
                department = "Plumbing"
            elif user["role"] == "ventilation_coordinator":
                department = "Ventilation"
            elif user["role"] == "employee":
                department = "External Wall Insulation"
            else:
                department = "Administration"
            db.execute(
                """
                INSERT OR IGNORE INTO user_departments
                    (user_id, department_id, is_primary)
                VALUES (?, ?, 1)
                """,
                (user["id"], department_ids[department]),
            )
        ewi_department_id = department_ids["External Wall Insulation"]
        ewi_supervisor = db.execute(
            """
            SELECT id FROM app_users
            WHERE role = 'supervisor' AND supervisor_specialty = ?
            ORDER BY id LIMIT 1
            """,
            ("External Wall Insulation",),
        ).fetchone()
        if ewi_supervisor is not None:
            for teamlead in db.execute(
                """
                SELECT users.id FROM app_users users
                JOIN user_departments departments ON departments.user_id = users.id
                WHERE users.role = 'teamlead'
                  AND users.team_name = ''
                  AND departments.department_id = ?
                """,
                (ewi_department_id,),
            ):
                if teamlead["id"] != ewi_supervisor["id"]:
                    db.execute(
                        """
                        INSERT OR IGNORE INTO user_reporting_lines
                            (manager_user_id, report_user_id, relationship)
                        VALUES (?, ?, 'supervisor')
                        """,
                        (ewi_supervisor["id"], teamlead["id"]),
                    )


def _validate_user_details(username, display_name):
    username = str(username or "").strip()
    display_name = str(display_name or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9._-]{3,64}", username):
        raise ApiError(
            400, "Username must be 3-64 characters: letters, numbers, dot, dash or underscore."
        )
    if not display_name or len(display_name) > 120:
        raise ApiError(400, "Enter a display name (maximum 120 characters).")
    return username, display_name


def _validate_pin(pin):
    if not isinstance(pin, str) or not re.fullmatch(r"[0-9]{6}", pin):
        raise ApiError(400, "PIN must be exactly 6 digits.")
    return pin


def _credential_digest(credential, salt, iterations=CREDENTIAL_ITERATIONS):
    return hashlib.pbkdf2_hmac(
        "sha256", credential.encode("utf-8"), salt, iterations
    )


def _role_has_access(db, role_key, profile):
    return db.execute(
        """
        SELECT 1 FROM app_role_permissions
        WHERE role_key = ? AND permission_key = ? AND is_allowed = 1
        """,
        (role_key, f"access.{profile}"),
    ).fetchone() is not None


def _insert_user(
    db, username, display_name, pin, role, supervisor_specialty="", team_name=""
):
    username, display_name = _validate_user_details(username, display_name)
    if not isinstance(role, str) or db.execute(
        "SELECT 1 FROM app_roles WHERE role_key = ? AND is_active = 1",
        (role,),
    ).fetchone() is None:
        raise ApiError(400, "Choose a valid account role.")
    if not isinstance(supervisor_specialty, str):
        raise ApiError(400, "Supervisor specialty must be text.")
    supervisor_specialty = supervisor_specialty.strip()
    if len(supervisor_specialty) > 120:
        raise ApiError(400, "Supervisor specialty must be 120 characters or fewer.")
    if not _role_has_access(db, role, "supervisor") and supervisor_specialty:
        raise ApiError(400, "Only Supervisor accounts can have a specialty.")
    if not isinstance(team_name, str):
        raise ApiError(400, "Team name must be text.")
    team_name = team_name.strip()
    if len(team_name) > 120:
        raise ApiError(400, "Team name must be 120 characters or fewer.")
    if not _role_has_access(db, role, "teamlead") and team_name:
        raise ApiError(400, "Only Teamlead accounts can have a team name.")
    pin = _validate_pin(pin)
    salt = secrets.token_bytes(16)
    password_hash = _credential_digest(pin, salt)
    cursor = db.execute(
        """
        INSERT INTO app_users
            (username, display_name, role, password_salt, password_hash, password_iterations,
             credential_type, supervisor_specialty, team_name)
        VALUES (?, ?, ?, ?, ?, ?, 'pin', ?, ?)
        """,
        (
            username,
            display_name,
            role,
            salt,
            password_hash,
            CREDENTIAL_ITERATIONS,
            supervisor_specialty,
            team_name,
        ),
    )
    user_id = cursor.lastrowid
    default_department = {
        "admin": "Administration",
        "assign": "Administration",
        "surveyor": "Surveyors",
        "delivery": "Delivery",
        "supervisor": supervisor_specialty or "Administration",
        "teamlead": team_name or "External Wall Insulation",
        "plumber": "Plumbing",
        "plumbing_coordinator": "Plumbing",
        "ventilation_coordinator": "Ventilation",
        "employee": "External Wall Insulation",
    }.get(role, "Administration")
    department = db.execute(
        "SELECT id FROM departments WHERE name = ? COLLATE NOCASE",
        (default_department,),
    ).fetchone()
    if department is None:
        department = db.execute(
            "SELECT id FROM departments WHERE name = 'Administration'"
        ).fetchone()
    db.execute(
        """
        INSERT INTO user_departments(user_id, department_id, is_primary)
        VALUES (?, ?, 1)
        """,
        (user_id, department["id"]),
    )
    return user_id


def _public_user(row):
    user = {
        "id": row["id"],
        "username": row["username"],
        "display_name": row["display_name"],
        "role": row["role"],
    }
    if "role_name" in row.keys():
        user["role_name"] = row["role_name"]
    if "supervisor_specialty" in row.keys():
        user["supervisor_specialty"] = row["supervisor_specialty"]
    if "team_name" in row.keys():
        user["team_name"] = row["team_name"]
    if "role_permissions" in row.keys():
        user["permissions"] = [
            permission
            for permission in (row["role_permissions"] or "").split("\x1f")
            if permission
        ]
    if "departments_json" in row.keys():
        try:
            user["departments"] = json.loads(row["departments_json"] or "[]")
        except (TypeError, json.JSONDecodeError):
            raise ApiError(500, "Stored user department data is invalid.")
    return user


def _user_record(db, user_id):
    return db.execute(
        """
        SELECT users.*, roles.display_name AS role_name,
               COALESCE((
                   SELECT group_concat(permission_key, char(31))
                   FROM app_role_permissions
                   WHERE role_key = users.role AND is_allowed = 1
               ), '') AS role_permissions,
               COALESCE((
                   SELECT json_group_array(json_object(
                       'id', departments.id,
                       'name', departments.name,
                       'is_primary', memberships.is_primary
                   ))
                   FROM user_departments memberships
                   JOIN departments ON departments.id = memberships.department_id
                   WHERE memberships.user_id = users.id
               ), '[]') AS departments_json
        FROM app_users users
        LEFT JOIN app_roles roles ON roles.role_key = users.role
        WHERE users.id = ?
        """,
        (user_id,),
    ).fetchone()


def _has_access_profile(user, role_key):
    if user is None:
        return False
    permissions = user["role_permissions"] if "role_permissions" in user.keys() else ""
    values = set((permissions or "").split("\x1f"))
    return f"access.{role_key}" in values or (
        role_key == "admin" and "*" in values
    )


def _in_department(db, user, department_name):
    return db.execute(
        """
        SELECT 1 FROM user_departments memberships
        JOIN departments ON departments.id = memberships.department_id
        WHERE memberships.user_id = ?
          AND departments.name = ? COLLATE NOCASE
        """,
        (user["id"], department_name),
    ).fetchone() is not None


def _is_plumbing_supervisor(db, user):
    return _has_access_profile(user, "supervisor") and (
        (user["supervisor_specialty"] or "").strip().casefold() == "plumbing"
        or db.execute(
            """
            SELECT 1 FROM user_departments memberships
            JOIN departments ON departments.id = memberships.department_id
            WHERE memberships.user_id = ?
              AND departments.name = 'Plumbing' COLLATE NOCASE
              AND departments.is_active = 1
            """,
            (user["id"],),
        ).fetchone()
        is not None
    )


def _plumbing_supervisor_assigned(db, house_id, user_id):
    return db.execute(
        """
        SELECT 1
        FROM provided_houses provided
        WHERE provided.id = ?
          AND provided.plan_plumber_required = 1
          AND (
              EXISTS (
                  SELECT 1 FROM house_plumbing_assignments assignment
                  WHERE assignment.house_id = provided.id
                    AND assignment.supervisor_user_id = ?
              )
              OR (
                  provided.plan_plumber_done_at != ''
                  AND provided.plan_plumber_done_by_user_id = ?
                  AND NOT EXISTS (
                      SELECT 1 FROM house_plumbing_assignments assignment
                      WHERE assignment.house_id = provided.id
                  )
              )
          )
        """,
        (house_id, user_id, user_id),
    ).fetchone() is not None


def _is_ewi_supervisor(db, user):
    return _has_access_profile(user, "supervisor") and (
        (
            user["supervisor_specialty"]
            if "supervisor_specialty" in user.keys()
            else ""
        )
        .strip()
        .casefold()
        == "external wall insulation".casefold()
        or db.execute(
            """
            SELECT 1 FROM user_departments memberships
            JOIN departments ON departments.id = memberships.department_id
            WHERE memberships.user_id = ?
              AND departments.name = 'External Wall Insulation' COLLATE NOCASE
              AND departments.is_active = 1
            """,
            (user["id"],),
        ).fetchone()
        is not None
    )


def _teamlead_ewi_houses(db, teamlead_user_id):
    return [
        dict(row)
        for row in db.execute(
            """
            SELECT assignment.house_id AS id, p.postcode, p.address, p.owner_name,
                   h.is_finished, assignment.assigned_by, assignment.assigned_at,
                   assignment.completed_by, assignment.completed_at,
                   supervisor.display_name AS supervisor_name
            FROM house_ewi_assignments assignment
            JOIN provided_houses p ON p.id = assignment.house_id
            LEFT JOIN houses h ON h.id = assignment.house_id
            LEFT JOIN house_role_assignments supervisor_assignment
              ON supervisor_assignment.house_id = assignment.house_id
             AND supervisor_assignment.role = 'supervisor'
            LEFT JOIN app_users supervisor
              ON supervisor.id = supervisor_assignment.user_id
            JOIN user_reporting_lines reports
              ON reports.manager_user_id = assignment.supervisor_user_id
             AND reports.report_user_id = assignment.teamlead_user_id
            WHERE assignment.teamlead_user_id = ?
              AND supervisor_assignment.user_id = assignment.supervisor_user_id
            ORDER BY (assignment.completed_at != ''), assignment.assigned_at DESC,
                     assignment.house_id COLLATE NOCASE
            """,
            (teamlead_user_id,),
        )
    ]


def _attic_cavity_quantities(db, house_ids):
    if not house_ids or not db.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' "
        "AND name = 'seai_applications'"
    ).fetchone():
        return {}
    placeholders = ",".join("?" for _ in house_ids)
    quantities = {}
    for row in db.execute(
        "SELECT application_id, detail_data_json FROM seai_applications "
        f"WHERE application_id IN ({placeholders})",
        house_ids,
    ):
        try:
            details = json.loads(row["detail_data_json"])
        except (TypeError, ValueError) as exc:
            raise ApiError(500, "Stored SEAI insulation details are invalid.") from exc
        if not isinstance(details, dict) or not isinstance(details.get("Measures"), list):
            continue
        work = {}
        for measure in details["Measures"]:
            if not isinstance(measure, dict):
                continue
            name = str(measure.get("Name") or "").strip().casefold()
            work_type = (
                "attic"
                if "attic" in name and "insulation" in name
                else "cavity"
                if "cavity wall" in name and "insulation" in name
                else None
            )
            if work_type is None:
                continue
            raw_quantity = measure.get("Survey Quantity")
            if isinstance(raw_quantity, bool):
                continue
            try:
                quantity = float(raw_quantity)
            except (TypeError, ValueError, OverflowError):
                continue
            if math.isfinite(quantity) and quantity > 0:
                work[work_type] = str(raw_quantity).strip()
        if work:
            quantities[row["application_id"]] = work
    return quantities


def _teamlead_insulation_houses(db, teamlead_user_id):
    assignments = [
        dict(row)
        for row in db.execute(
            """
            SELECT assignment.house_id AS id, assignment.work_type,
                   p.postcode, p.address, p.owner_name,
                   assignment.assigned_by, assignment.assigned_at,
                   assignment.completed_by, assignment.completed_at,
                   p.ready_for_start,
                   COALESCE(active.is_finished, 0) AS is_finished
            FROM house_insulation_assignments assignment
            JOIN provided_houses p ON p.id = assignment.house_id
            LEFT JOIN houses active ON active.id = p.id
            WHERE assignment.teamlead_user_id = ?
            ORDER BY (assignment.completed_at != ''), assignment.assigned_at DESC,
                     assignment.house_id COLLATE NOCASE, assignment.work_type
            """,
            (teamlead_user_id,),
        )
    ]
    quantities = _attic_cavity_quantities(
        db, list({assignment["id"] for assignment in assignments})
    )
    return [
        {
            **assignment,
            "survey_quantity": quantities.get(assignment["id"], {}).get(
                assignment["work_type"], ""
            ),
        }
        for assignment in assignments
        if assignment["ready_for_start"]
        and not assignment["is_finished"]
        and quantities.get(assignment["id"], {}).get(assignment["work_type"])
    ]


def _external_insulation_supervisors(db):
    return db.execute(
        """
        SELECT users.id
        FROM app_users users
        WHERE users.is_active = 1
          AND users.supervisor_specialty = 'External Wall Insulation' COLLATE NOCASE
          AND EXISTS (
              SELECT 1 FROM app_role_permissions access
              WHERE access.role_key = users.role
                AND access.permission_key = 'access.supervisor'
                AND access.is_allowed = 1
          )
        ORDER BY users.id
        """
    ).fetchall()


def _can_complete_work_plan_trade(db, user, trade):
    if trade == "plumber":
        return _has_access_profile(user, "plumber")
    if trade == "vent":
        return _in_department(db, user, "Ventilation")
    if _has_access_profile(user, "assign"):
        return True
    if not (
        _has_access_profile(user, "supervisor")
        or _has_access_profile(user, "teamlead")
    ):
        return False
    return db.execute(
        """
        SELECT 1 FROM app_users users
        WHERE users.id = ?
          AND (
              users.supervisor_specialty = 'External Wall Insulation' COLLATE NOCASE
              OR EXISTS (
                  SELECT 1 FROM user_departments memberships
                  JOIN departments ON departments.id = memberships.department_id
                  WHERE memberships.user_id = users.id
                    AND departments.name = 'External Wall Insulation' COLLATE NOCASE
              )
          )
        """,
        (user["id"],),
    ).fetchone() is not None


def _require_work_plan_checker(db, user, house_id, item):
    if item not in (*WORK_PLAN_TRADES, *WORK_PLAN_SURVEY_QUESTIONS):
        raise ApiError(400, "Choose a valid work plan item.")
    completion = db.execute(
        f"""
        SELECT plan_{item}_done_by AS done_by,
               plan_{item}_done_at AS done_at,
               plan_{item}_done_by_user_id AS done_by_user_id
        FROM provided_houses
        WHERE id = ?
        """,
        (house_id,),
    ).fetchone()
    if completion and (
        completion["done_by"] or completion["done_at"]
    ) and completion["done_by_user_id"] != user["id"]:
        raise ApiError(
            403,
            "Only the original checker can change this work-plan status.",
        )
    return completion


def _set_user_organization(db, user_id, department_ids, manager_user_id=None):
    if not isinstance(department_ids, list) or not department_ids:
        raise ApiError(400, "Select at least one department for each user.")
    normalized_ids = []
    for department_id in department_ids:
        if isinstance(department_id, bool):
            raise ApiError(400, "Choose valid departments.")
        try:
            department_id = int(department_id)
        except (TypeError, ValueError):
            raise ApiError(400, "Choose valid departments.") from None
        if department_id not in normalized_ids:
            normalized_ids.append(department_id)
    placeholders = ",".join("?" for _ in normalized_ids)
    departments = db.execute(
        f"SELECT id FROM departments WHERE is_active = 1 AND id IN ({placeholders})",
        normalized_ids,
    ).fetchall()
    if len(departments) != len(normalized_ids):
        raise ApiError(400, "Choose active departments.")
    db.execute("DELETE FROM user_departments WHERE user_id = ?", (user_id,))
    db.executemany(
        """
        INSERT INTO user_departments(user_id, department_id, is_primary)
        VALUES (?, ?, ?)
        """,
        (
            (user_id, department_id, int(index == 0))
            for index, department_id in enumerate(normalized_ids)
        ),
    )
    db.execute(
        "DELETE FROM user_reporting_lines WHERE report_user_id = ?", (user_id,)
    )
    if manager_user_id in (None, ""):
        return
    if isinstance(manager_user_id, bool):
        raise ApiError(400, "Choose a valid manager.")
    try:
        manager_user_id = int(manager_user_id)
    except (TypeError, ValueError):
        raise ApiError(400, "Choose a valid manager.") from None
    if manager_user_id == user_id:
        raise ApiError(400, "A user cannot manage themselves.")
    manager = db.execute(
        """
        SELECT users.id, users.role FROM app_users users
        WHERE users.id = ? AND users.is_active = 1
          AND EXISTS (
              SELECT 1 FROM app_role_permissions access
              WHERE access.role_key = users.role
                AND access.is_allowed = 1
                AND access.permission_key IN (
                    '*', 'access.admin', 'access.supervisor', 'access.teamlead'
                )
          )
        """,
        (manager_user_id,),
    ).fetchone()
    if manager is None:
        raise ApiError(400, "Choose an active Admin, Supervisor, or Teamlead.")
    report_role = db.execute(
        "SELECT role FROM app_users WHERE id = ?", (user_id,)
    ).fetchone()["role"]
    manager_is_admin = _role_has_access(db, manager["role"], "admin") or (
        db.execute(
            """
            SELECT 1 FROM app_role_permissions
            WHERE role_key = ? AND permission_key = '*' AND is_allowed = 1
            """,
            (manager["role"],),
        ).fetchone()
        is not None
    )
    manager_is_supervisor = _role_has_access(db, manager["role"], "supervisor")
    manager_is_teamlead = _role_has_access(db, manager["role"], "teamlead")
    report_is_assign = _role_has_access(db, report_role, "assign")
    report_is_teamlead = _role_has_access(db, report_role, "teamlead")
    report_is_team_member = any(
        _role_has_access(db, report_role, profile)
        for profile in ("employee", "plumber")
    )
    if report_is_assign:
        if not manager_is_admin:
            raise ApiError(409, "Assign accounts can report only to an Admin.")
    elif report_is_teamlead:
        if not manager_is_supervisor:
            raise ApiError(409, "Teamleads can report only to a Supervisor.")
    elif report_is_team_member:
        if not manager_is_teamlead:
            raise ApiError(409, "Team members can report only to a Teamlead.")
    else:
        raise ApiError(
            409,
            "Reporting lines must connect Supervisors to Teamleads or Teamleads to team members.",
        )
    shared_department = db.execute(
        f"""
        SELECT 1
        FROM user_departments memberships
        WHERE memberships.user_id = ?
          AND memberships.department_id IN ({placeholders})
        LIMIT 1
        """,
        [manager_user_id, *normalized_ids],
    ).fetchone()
    if shared_department is None:
        raise ApiError(409, "The manager and user must share a department.")
    cycle = db.execute(
        """
        WITH RECURSIVE reports(user_id) AS (
            SELECT report_user_id FROM user_reporting_lines
            WHERE manager_user_id = ?
            UNION
            SELECT lines.report_user_id
            FROM user_reporting_lines lines
            JOIN reports ON lines.manager_user_id = reports.user_id
        )
        SELECT 1 FROM reports WHERE user_id = ? LIMIT 1
        """,
        (user_id, manager_user_id),
    ).fetchone()
    if cycle:
        raise ApiError(409, "This reporting line would create a management cycle.")
    db.execute(
        """
        INSERT INTO user_reporting_lines(manager_user_id, report_user_id, relationship)
        VALUES (?, ?, ?)
        """,
        (
            manager_user_id,
            user_id,
            "administrator" if report_is_assign else "manager",
        ),
    )


def _can_manage_team_roster(db, user, teamlead_id):
    if _has_access_profile(user, "admin"):
        return True
    if (
        _has_access_profile(user, "teamlead")
        and user["id"] == teamlead_id
    ):
        return True
    if not _has_access_profile(user, "supervisor"):
        return False
    return db.execute(
        """
        SELECT 1
        FROM user_reporting_lines lines
        JOIN app_users reports ON reports.id = lines.report_user_id
        JOIN app_role_permissions access ON access.role_key = reports.role
        WHERE lines.manager_user_id = ?
          AND lines.report_user_id = ?
          AND reports.is_active = 1
          AND access.permission_key = 'access.teamlead'
          AND access.is_allowed = 1
        LIMIT 1
        """,
        (user["id"], teamlead_id),
    ).fetchone() is not None


def _new_session(db, user_id):
    db.execute("DELETE FROM app_sessions WHERE expires_at <= ?", (int(datetime.now().timestamp()),))
    token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode("ascii")).hexdigest()
    expires_at = int(datetime.now().timestamp()) + SESSION_MAX_AGE
    db.execute(
        "INSERT INTO app_sessions(token_hash, user_id, expires_at) VALUES (?, ?, ?)",
        (token_hash, user_id, expires_at),
    )
    return token, token_hash, expires_at


def _verify_credential(row, credential):
    if not isinstance(credential, str) or len(credential) > 1024:
        credential = ""
    if row is None:
        salt = b"material-tracker-dummy-salt"
        expected = _credential_digest(credential, salt)
        hmac.compare_digest(expected, b"\0" * len(expected))
        return False
    actual = _credential_digest(
        credential, row["password_salt"], row["password_iterations"]
    )
    return hmac.compare_digest(actual, row["password_hash"])


def _user_count(db):
    return db.execute("SELECT COUNT(*) FROM app_users").fetchone()[0]


def _login_attempt_key(username):
    normalized = str(username or "").strip().casefold()[:64]
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _check_login_attempts(db, username_hash, now):
    row = db.execute(
        "SELECT locked_until FROM app_login_attempts WHERE username_hash = ?",
        (username_hash,),
    ).fetchone()
    if row and row["locked_until"] > now:
        raise ApiError(429, "Too many incorrect attempts. Try again in 15 minutes.")


def _record_failed_login(db, username_hash, now):
    row = db.execute(
        """
        SELECT failures, window_started_at FROM app_login_attempts
        WHERE username_hash = ?
        """,
        (username_hash,),
    ).fetchone()
    if row is None or now - row["window_started_at"] >= 900:
        failures = 1
        window_started_at = now
    else:
        failures = row["failures"] + 1
        window_started_at = row["window_started_at"]
    locked_until = now + 900 if failures >= 5 else 0
    db.execute(
        """
        INSERT INTO app_login_attempts
            (username_hash, failures, window_started_at, locked_until)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(username_hash) DO UPDATE SET
            failures = excluded.failures,
            window_started_at = excluded.window_started_at,
            locked_until = excluded.locked_until
        """,
        (username_hash, failures, window_started_at, locked_until),
    )


def _clear_login_attempts(db, username_hash):
    db.execute(
        "DELETE FROM app_login_attempts WHERE username_hash = ?",
        (username_hash,),
    )


def location_key(kind, reference=""):
    if kind == "warehouse":
        return "warehouse"
    if kind == "team":
        return team_location(reference)
    if kind not in LOCATION_TYPES or not reference or ":" in reference:
        raise ApiError(400, "Choose a valid team or house location.")
    return f"{kind}:{reference.strip()}"


def team_location(name, department=""):
    name = str(name or "").strip()
    department = str(department or "").strip()
    if not name:
        raise ApiError(400, "Team name is required.")
    if department:
        return f"team:{quote(department, safe='')}|{quote(name, safe='')}"
    if ":" in name:
        raise ApiError(400, "Team names cannot contain a colon.")
    if "|" in name:
        return f"team:|{quote(name, safe='')}"
    return f"team:{name}"


def employee_location(name):
    return f"employee:{quote(str(name or '').strip(), safe='')}"


def canonical_department(db, name):
    name = str(name or "").strip()
    if not name:
        return ""
    existing = db.execute("SELECT DISTINCT department FROM movements").fetchall()
    for row in existing:
        if row["department"].casefold() == name.casefold():
            return row["department"]
    return name


def canonical_team(db, name, department=""):
    name = str(name or "").strip()
    if not name:
        return ""
    existing = db.execute(
        "SELECT team, department FROM movements WHERE team != ''"
    ).fetchall()
    for row in existing:
        if (
            row["team"].casefold() == name.casefold()
            and row["department"].casefold() == department.casefold()
        ):
            return row["team"]
    return name


def parse_quantity(value, label="Quantity", allow_zero=False):
    try:
        qty = float(value)
    except (TypeError, ValueError):
        raise ApiError(400, f"{label} must be a number.")
    if not math.isfinite(qty) or qty < 0 or (qty == 0 and not allow_zero):
        condition = "zero or greater" if allow_zero else "greater than zero"
        raise ApiError(400, f"{label} must be {condition}.")
    return qty


def parse_legacy_date(value, row_number):
    if isinstance(value, (datetime, date)):
        return value.date().isoformat() if isinstance(value, datetime) else value.isoformat()
    text = str(value or "").strip()
    for pattern in ("%Y-%m-%d", "%d/%m/%Y", "%d/%m/%y", "%d-%m-%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(text, pattern).date().isoformat()
        except ValueError:
            continue
    raise ApiError(400, f"MOVEMENTS row {row_number} has an invalid date.")


def known_locations(db):
    locations = {"warehouse"}
    locations.update(
        f"house:{row['id']}" for row in db.execute("SELECT id FROM houses")
    )
    for row in db.execute(
        "SELECT DISTINCT team, department FROM movements WHERE team != ''"
    ):
        locations.add(team_location(row["team"], row["department"]))
    for row in db.execute(
        "SELECT DISTINCT employee_name FROM houses WHERE TRIM(employee_name) != ''"
    ):
        locations.add(employee_location(row["employee_name"]))
    for row in db.execute("SELECT source, destination FROM movements"):
        locations.add(row["source"])
        locations.add(row["destination"])
    for row in db.execute("SELECT location FROM stocktakes"):
        locations.add(row["location"])
    return locations


def balances_for(db, material_id):
    material = db.execute(
        "SELECT opening_qty FROM materials WHERE id = ?", (material_id,)
    ).fetchone()
    if material is None:
        raise ApiError(404, "Material not found.")

    locations = known_locations(db)
    locations.add("warehouse")
    balances = {}
    movements = db.execute(
        "SELECT id, qty, source, destination FROM movements "
        "WHERE material_id = ? ORDER BY id",
        (material_id,),
    ).fetchall()
    for location in locations:
        snapshot = db.execute(
            "SELECT quantity, movement_id FROM stocktakes "
            "WHERE material_id = ? AND location = ? ORDER BY id DESC LIMIT 1",
            (material_id, location),
        ).fetchone()
        if snapshot:
            balance = snapshot["quantity"]
            after_id = snapshot["movement_id"]
        elif location == "warehouse":
            balance = material["opening_qty"]
            after_id = 0
        else:
            balance = 0.0
            after_id = 0

        if balance is not None:
            for movement in movements:
                if movement["id"] <= after_id:
                    continue
                if movement["source"] == location:
                    balance -= movement["qty"]
                if movement["destination"] == location:
                    balance += movement["qty"]
        balances[location] = balance
    return balances


def _valid_reference(db, location):
    kind, _, reference = location.partition(":")
    if location in {"warehouse", "consumed"}:
        return
    if kind == "house":
        exists = db.execute("SELECT 1 FROM houses WHERE id = ?", (reference,)).fetchone()
    elif kind == "team" and reference.strip():
        exists = True
    else:
        exists = False
    if not exists:
        raise ApiError(400, "A selected house or team location is invalid.")


CLOSEOUT_NOTE = "Automatically recorded from remaining house stock when the house was marked finished."


def _record_house_status(db, house_id, status, changed_by):
    changed_at = datetime.now().isoformat(timespec="seconds")
    changed_by = str(changed_by or "").strip()
    db.execute(
        """
        INSERT INTO house_status_history(house_id, status, changed_by, changed_at)
        VALUES (?, ?, ?, ?)
        """,
        (
            house_id,
            status,
            changed_by,
            changed_at,
        ),
    )
    return {
        "status": status,
        "changed_by": changed_by,
        "changed_at": changed_at,
    }


def _find_seai_application_id(value, depth=0):
    if not isinstance(value, (dict, list)) or depth > 5:
        return ""
    items = value.items() if isinstance(value, dict) else enumerate(value)
    for key, item in items:
        if (isinstance(key, str) and re.fullmatch(r"application[\s_]*id", key, re.I)
                and item and not isinstance(item, (dict, list))):
            return str(item).strip()
    for _, item in (value.items() if isinstance(value, dict) else enumerate(value)):
        found = _find_seai_application_id(item, depth + 1)
        if found:
            return found
    return ""


def _seai_application_ids_by_house(db, house_ids):
    if not house_ids or not db.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' "
        "AND name = 'seai_applications'"
    ).fetchone():
        return {}
    placeholders = ",".join("?" for _ in house_ids)
    application_ids = {}
    for row in db.execute(
        "SELECT application_id, detail_data_json FROM seai_applications "
        f"WHERE application_id IN ({placeholders})",
        house_ids,
    ):
        try:
            details = json.loads(row["detail_data_json"])
        except (TypeError, ValueError):
            continue
        application_id = _find_seai_application_id(details)
        if application_id:
            application_ids[row["application_id"]] = application_id
    return application_ids


def _attach_seai_application_ids(db, houses):
    application_ids = _seai_application_ids_by_house(
        db, [house["id"] for house in houses]
    )
    for house in houses:
        house["seai_application_id"] = application_ids.get(house["id"], "")
    return houses


def _attach_seai_application_summaries(db, houses):
    if not houses:
        return houses
    has_seai_table = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' "
        "AND name = 'seai_applications'"
    ).fetchone()
    if not has_seai_table:
        return houses
    house_ids = [house["id"] for house in houses]
    placeholders = ",".join("?" for _ in house_ids)
    summaries = {}
    for row in db.execute(
        "SELECT application_id, detail_data_json FROM seai_applications "
        f"WHERE application_id IN ({placeholders})",
        house_ids,
    ):
        try:
            summary = _seai_card_summary(json.loads(row["detail_data_json"]))
        except (TypeError, ValueError):
            summary = None
        if summary:
            summaries[row["application_id"]] = summary
    for house in houses:
        house["seai_summary"] = summaries.get(house["id"])
    return houses


def _seai_card_summary(details):
    if not isinstance(details, dict):
        return None
    application = details.get("Application Details")
    prop = details.get("Property Details")
    application = application if isinstance(application, dict) else {}
    prop = prop if isinstance(prop, dict) else {}
    application_id = application.get("Application ID") or _find_seai_application_id(details)
    measures = []
    for measure in details.get("Measures") or []:
        if isinstance(measure, dict):
            measures.append(
                {
                    key: measure.get(key)
                    for key in ("Name", "Survey Quantity", "Survey Qty including Variation")
                }
            )
    return {
        "Application Details": {
            "Application ID": application_id or "",
            "Applicant Name": application.get("Applicant Name") or "",
        },
        "Property Details": {
            "Eircode": prop.get("Eircode") or "",
            "Address Line 1": prop.get("Address Line 1") or "",
        },
        "Measures": measures,
    }


def _house_audit_by_id(db):
    audit = {}
    for row in db.execute(
        """
        SELECT house_id, status, changed_by, changed_at
        FROM house_status_history
        ORDER BY id
        """
    ):
        if row["status"] not in {"ready", "not_started", "active", "closed"}:
            continue
        entry = {
            "type": "status",
            "status": row["status"],
            "changed_by": row["changed_by"],
            "changed_at": row["changed_at"],
        }
        house_audit = audit.setdefault(
            row["house_id"],
            {"status_history": [], "activity_history": [], "last_movement": None},
        )
        house_audit["status_history"].append(entry)
        house_audit["activity_history"].append(entry)

    for row in db.execute(
        """
        SELECT house_id, created_at, created_by
        FROM material_requests
        ORDER BY id
        """
    ):
        audit.setdefault(
            row["house_id"],
            {"status_history": [], "activity_history": [], "last_movement": None},
        )["activity_history"].append(
            {
                "type": "material_request",
                "created_by": row["created_by"],
                "created_at": row["created_at"],
            }
        )

    for row in db.execute(
        "SELECT house_id, updated_by, updated_at FROM house_surveys"
    ):
        audit.setdefault(
            row["house_id"],
            {"status_history": [], "activity_history": [], "last_movement": None},
        )["activity_history"].append(
            {
                "type": "survey",
                "changed_by": row["updated_by"],
                "changed_at": row["updated_at"],
            }
        )

    latest_movements = {}
    for row in db.execute(
        """
        SELECT m.id, m.house_id, m.kind, m.material_id, p.name AS material,
               p.unit, m.qty, m.delivery_number, m.person, m.team, m.department,
               m.happened_on, m.recorded_at, m.source, m.destination,
               COALESCE(h.employee_name, '') AS house_employee
        FROM movements m
        JOIN materials p ON p.id = m.material_id
        LEFT JOIN houses h ON h.id = m.house_id
        WHERE m.house_id IS NOT NULL
           OR m.source LIKE 'house:%'
           OR m.destination LIKE 'house:%'
        ORDER BY m.id
        """
    ):
        movement = dict(row)
        related_house_ids = {
            movement["house_id"],
            movement["source"][6:] if movement["source"].startswith("house:") else None,
            movement["destination"][6:] if movement["destination"].startswith("house:") else None,
        }
        for house_id in filter(None, related_house_ids):
            house_audit = audit.setdefault(
                house_id,
                {"status_history": [], "activity_history": [], "last_movement": None},
            )
            house_employee = movement["house_employee"]
            if not house_employee:
                assigned = db.execute(
                    "SELECT employee_name FROM houses WHERE id = ?",
                    (house_id,),
                ).fetchone()
                house_employee = assigned["employee_name"] if assigned else ""
            house_audit["activity_history"].append(
                {"type": "movement", **movement, "house_employee": house_employee}
            )
            current = latest_movements.get(house_id)
            if current is None or (movement["recorded_at"], movement["id"]) > (
                current["recorded_at"],
                current["id"],
            ):
                latest_movements[house_id] = movement
    for house_id, movement in latest_movements.items():
        audit[house_id]["last_movement"] = movement
    for house_audit in audit.values():
        house_audit["activity_history"].sort(
            key=lambda entry: (
                entry.get("changed_at")
                or entry.get("created_at")
                or entry.get("recorded_at")
                or "",
                entry.get("id") or 0,
            ),
            reverse=True,
        )
    return audit


def _undo_auto_closeouts(db, house_id):
    """Reopening a house puts back the stock that closing it wrote off automatically."""
    cursor = db.execute(
        "DELETE FROM movements WHERE house_id = ? AND kind = 'use' AND notes = ?",
        (house_id, CLOSEOUT_NOTE),
    )
    return cursor.rowcount


def _require_ready_to_activate(db, house_id):
    """A listed SEAI house needs readiness and a recorded survey before activation."""
    row = db.execute(
        "SELECT p.ready_for_start, h.id AS active_id, hs.house_id AS surveyed_id "
        "FROM provided_houses p "
        "LEFT JOIN houses h ON h.id = p.id "
        "LEFT JOIN house_surveys hs ON hs.house_id = p.id WHERE p.id = ?",
        (str(house_id).strip(),),
    ).fetchone()
    if row and row["active_id"] is None:
        if not row["ready_for_start"]:
            raise ApiError(
                409,
                f"House {str(house_id).strip()} is not marked ready for start. "
                "Mark it ready in Houses & teams before activating it.",
            )
        if not row["surveyed_id"]:
            raise ApiError(
                409,
                "Surveyor review is required before activation. "
                "Record the house survey in the Surveyor room first.",
            )


def _upsert_house(db, house_id, postcode="", owner_name="", employee_name=""):
    if not house_id or not str(house_id).strip():
        raise ApiError(400, "House ID is required for this movement.")
    house_id = str(house_id).strip()
    db.execute(
        """
        INSERT INTO houses(id, postcode, owner_name, employee_name, status_changed_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            postcode = CASE WHEN excluded.postcode = '' THEN houses.postcode ELSE excluded.postcode END,
            owner_name = CASE WHEN excluded.owner_name = '' THEN houses.owner_name ELSE excluded.owner_name END,
            employee_name = CASE WHEN excluded.employee_name = '' THEN houses.employee_name ELSE excluded.employee_name END
        """,
        (
            house_id,
            str(postcode or "").strip(),
            str(owner_name or "").strip(),
            str(employee_name or "").strip(),
            datetime.now().timestamp(),
        ),
    )
    db.execute(
        "INSERT OR IGNORE INTO provided_houses(id, postcode, owner_name, source) "
        "VALUES (?, ?, ?, 'tracker')",
        (house_id, str(postcode or "").strip(), str(owner_name or "").strip()),
    )
    db.execute(
        "UPDATE provided_houses SET ready_for_start = 0 WHERE id = ?", (house_id,)
    )
    db.execute(
        """
        INSERT OR IGNORE INTO house_checklists
            (house_id, scaffolding_status, esb_unclipped, plumber_status)
        SELECT id,
               CASE plan_scaffolding_required
                   WHEN 1 THEN 'not_done'
                   WHEN 0 THEN 'not_necessary'
                   ELSE plan_scaffolding
               END,
               CASE plan_esb_required
                   WHEN 1 THEN 'no'
                   WHEN 0 THEN 'not_esb'
                   ELSE plan_esb
               END,
               CASE plan_plumber_required
                   WHEN 1 THEN 'not_done'
                   WHEN 0 THEN 'not_necessary'
                   ELSE plan_plumber
               END
        FROM provided_houses
        WHERE id = ? AND (
            (plan_scaffolding_required IS NOT NULL
             AND plan_esb_required IS NOT NULL
             AND plan_plumber_required IS NOT NULL)
            OR (plan_scaffolding IS NOT NULL
                AND plan_esb IS NOT NULL
                AND plan_plumber IS NOT NULL)
        )
        """,
        (house_id,),
    )
    return house_id


def _next_delivery_number(db, house_id):
    if not house_id:
        raise ApiError(400, "House ID is required for a delivery.")
    return db.execute(
        """
        SELECT COALESCE(MAX(delivery_number), 0) + 1
        FROM movements
        WHERE house_id = ? AND kind IN ('delivery', 'pickup')
        """,
        (house_id,),
    ).fetchone()[0]


def _record_movement(
    db,
    record,
    check_balance=True,
    delivery_number=None,
    allow_house_updates=True,
):
    kind = str(record.get("kind", "")).strip()
    if kind not in MOVEMENT_TYPES:
        raise ApiError(400, "Choose a valid movement type.")
    happened_on = str(record.get("date") or date.today().isoformat()).strip()
    try:
        datetime.strptime(happened_on, "%Y-%m-%d")
    except ValueError:
        raise ApiError(400, "Date must use YYYY-MM-DD.")

    material_id = record.get("material_id")
    material = db.execute(
        "SELECT id FROM materials WHERE id = ?", (material_id,)
    ).fetchone()
    if material is None:
        raise ApiError(400, "Choose a valid material.")
    qty = parse_quantity(record.get("qty"))
    department = canonical_department(db, record.get("department"))
    team = canonical_team(db, record.get("team"), department)
    person = str(record.get("person") or "").strip()
    notes = str(record.get("notes") or "").strip()
    if len(notes) > 2000:
        raise ApiError(400, "Notes must be 2,000 characters or fewer.")
    house_id = str(record.get("house_id") or "").strip()
    postcode = str(record.get("postcode") or "").strip()
    owner_name = str(record.get("owner_name") or "").strip()
    employee_name = str(record.get("employee_name") or "").strip()

    def movement_house(reference, postcode="", owner="", assigned_employee=""):
        house_reference = str(reference or "").strip()
        if not house_reference:
            raise ApiError(400, "House ID is required for this movement.")
        closed = db.execute(
            "SELECT is_finished FROM houses WHERE id = ? COLLATE NOCASE",
            (house_reference,),
        ).fetchone()
        if closed and closed["is_finished"]:
            raise ApiError(409, f"House {house_reference} is closed. Reopen it before recording movements.")
        _require_ready_to_activate(db, house_reference)
        if not allow_house_updates:
            exists = db.execute(
                "SELECT 1 FROM houses WHERE id = ?", (house_reference,)
            ).fetchone()
            if not exists:
                raise ApiError(403, "Employees can only record movements for existing houses.")
            return house_reference
        return _upsert_house(
            db, house_reference, postcode, owner, assigned_employee
        )

    if kind in {"pickup", "delivery", "use", "return"} and not house_id:
        raise ApiError(400, "House ID is required for this movement.")
    if house_id:
        house_id = movement_house(house_id, postcode, owner_name, employee_name)

    if kind == "collection":
        if not team or not person:
            raise ApiError(400, "Record the responsible person and team for every movement.")
        source, destination = "warehouse", team_location(team, department)
    elif kind == "pickup":
        if not team or not person:
            raise ApiError(400, "Record the responsible person and team for every movement.")
        source, destination = "warehouse", location_key("house", house_id)
    elif kind == "delivery":
        source_kind = record.get("source_kind", "warehouse")
        if source_kind not in {"warehouse", "team"}:
            raise ApiError(400, "A delivery must come from the warehouse or a team.")
        source_department = canonical_department(
            db, record.get("source_department") or department
        )
        source_team = canonical_team(
            db, record.get("source_team") or team, source_department
        )
        source = (
            "warehouse"
            if source_kind == "warehouse"
            else team_location(source_team, source_department)
        )
        destination = location_key("house", house_id)
        team = team or (source_team if source_kind == "team" else "")
        department = department or (source_department if source_kind == "team" else "")
    elif kind in {"use", "return"}:
        source = location_key("house", house_id)
        destination = "consumed" if kind == "use" else "warehouse"
    else:
        source_kind = str(record.get("source_kind") or "")
        destination_kind = str(record.get("destination_kind") or "")
        if source_kind not in {"team", "house"} or destination_kind not in {"team", "house"}:
            raise ApiError(400, "Transfers must be between a team and/or a house.")
        source_reference = str(record.get("source_reference") or "").strip()
        destination_reference = str(record.get("destination_reference") or "").strip()
        if source_kind == "house":
            movement_house(
                source_reference,
                record.get("source_postcode"),
            )
        if destination_kind == "house":
            movement_house(
                destination_reference,
                record.get("destination_postcode"),
            )
        if source_kind == "team":
            source_department = canonical_department(db, record.get("source_department"))
            source_reference = canonical_team(db, source_reference, source_department)
            source = team_location(source_reference, source_department)
        else:
            source = location_key(source_kind, source_reference)
        if destination_kind == "team":
            destination_department = canonical_department(
                db, record.get("destination_department")
            )
            destination_reference = canonical_team(
                db, destination_reference, destination_department
            )
            destination = team_location(destination_reference, destination_department)
        else:
            destination = location_key(destination_kind, destination_reference)
        if source == destination:
            raise ApiError(400, "Choose different source and destination locations.")
        if not house_id:
            if source_kind == "house":
                house_id = source_reference
            elif destination_kind == "house":
                house_id = destination_reference
        team = team or (
            source_reference if source_kind == "team" else (
                destination_reference if destination_kind == "team" else ""
            )
        )
        department = department or (
            source_department if source_kind == "team" else (
                destination_department if destination_kind == "team" else ""
            )
        )

    if not person or (not team and kind != "use"):
        raise ApiError(400, "Record the responsible person and team for every movement.")
    _valid_reference(db, source)
    _valid_reference(db, destination)
    if check_balance:
        available = balances_for(db, material_id).get(source)
        if available is None:
            raise ApiError(
                409,
                "The source balance is unknown. Record a stocktake before moving material from this location.",
            )
        if available + 1e-9 < qty:
            raise ApiError(
                409,
                f"Insufficient stock at the source location (available: {available:g}).",
            )

    cursor = db.execute(
        """
        INSERT INTO movements
            (happened_on, kind, material_id, qty, source, destination, house_id,
             delivery_number, person, team, department, notes, recorded_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            happened_on, kind, material_id, qty, source, destination, house_id or None,
            (
                delivery_number or _next_delivery_number(db, house_id)
                if kind in {"delivery", "pickup"}
                else None
            ),
            person, team, department, notes,
            datetime.now().isoformat(timespec="seconds"),
        ),
    )
    return cursor.lastrowid


def _record_movement_batch(db, payload, allow_house_updates=True):
    if not isinstance(payload, dict):
        raise ApiError(400, "Movement data must be a JSON object.")
    items = payload.get("items")
    if not isinstance(items, list) or not items:
        raise ApiError(400, "Add at least one material to this movement.")
    if len(items) > 200:
        raise ApiError(400, "A movement can include at most 200 materials.")

    batch_delivery_number = None
    if payload.get("kind") in {"delivery", "pickup"}:
        batch_delivery_number = _next_delivery_number(db, payload.get("house_id"))
    if payload.get("kind") == "pickup" and "is_extra_delivery" in payload:
        is_extra_delivery = payload["is_extra_delivery"]
        if not isinstance(is_extra_delivery, bool):
            raise ApiError(400, "Extra delivery must be selected or cleared.")
        if is_extra_delivery and batch_delivery_number == 1:
            raise ApiError(400, "Record the first warehouse pickup before marking an extra delivery.")
        if not is_extra_delivery and batch_delivery_number > 1:
            raise ApiError(400, "Mark this pickup as an extra delivery to record it as delivery #2 or later.")

    common = {key: value for key, value in payload.items() if key != "items"}
    movement_ids = []
    for item in items:
        if not isinstance(item, dict):
            raise ApiError(400, "Each material entry must be an object.")
        record = {
            **common,
            "material_id": item.get("material_id"),
            "qty": item.get("qty"),
        }
        movement_ids.append(
            _record_movement(
                db,
                record,
                delivery_number=batch_delivery_number,
                allow_house_updates=allow_house_updates,
            )
        )
    return movement_ids, batch_delivery_number


def _movement_json(row):
    return {
        "id": row["id"],
        "date": row["happened_on"],
        "recorded_at": row["recorded_at"],
        "kind": row["kind"],
        "material_id": row["material_id"],
        "material": row["material"],
        "qty": row["qty"],
        "source": row["source"],
        "destination": row["destination"],
        "house_id": row["house_id"],
        "house_owner": row["house_owner"],
        "house_employee": row["house_employee"],
        "house_postcode": row["house_postcode"],
        "delivery_number": row["delivery_number"],
        "person": row["person"],
        "team": row["team"],
        "department": row["department"],
        "notes": row["notes"],
    }


def _movement_rows(db, limit=100, query=""):
    query = str(query or "").strip()
    sql = """
        SELECT m.*, p.name AS material, h.owner_name AS house_owner,
            h.employee_name AS house_employee, h.postcode AS house_postcode
        FROM movements m
        JOIN materials p ON p.id = m.material_id
        LEFT JOIN houses h ON h.id = m.house_id
        WHERE m.kind != 'use'
    """
    if query:
        searchable = (
            "m.happened_on", "m.kind", "p.name", "m.source", "m.destination",
            "m.house_id", "h.owner_name", "h.employee_name", "h.postcode",
            "m.person", "m.team", "m.department", "m.notes",
        )
        sql += " AND (" + " OR ".join(
            "instr(lower(COALESCE({}, '')), lower(?)) > 0".format(column)
            for column in searchable
        ) + ")"
        sql += " ORDER BY m.id DESC"
        return db.execute(sql, (query,) * len(searchable)).fetchall()
    return db.execute(sql + " ORDER BY m.id DESC LIMIT ?", (limit,)).fetchall()


def _house_movement_rows(db, house_id):
    location = f"house:{house_id}"
    return db.execute(
        """
        SELECT m.*, p.name AS material, h.owner_name AS house_owner,
            h.employee_name AS house_employee, h.postcode AS house_postcode
        FROM movements m
        JOIN materials p ON p.id = m.material_id
        LEFT JOIN houses h ON h.id = m.house_id
        WHERE m.house_id = ? OR m.source = ? OR m.destination = ?
        ORDER BY m.happened_on DESC, m.id DESC
        """,
        (house_id, location, location),
    ).fetchall()


def _esb_unclipped_status(value):
    if value is None:
        return None
    if value in (1, True, "1", "yes"):
        return "yes"
    if value in (0, False, "0", "no"):
        return "no"
    return value


def _delivered_to_house(db, house_id):
    """Net quantity of each material sent to a house: deliveries in, minus anything sent back out (not consumed)."""
    location = f"house:{house_id}"
    delivered = {}
    for row in db.execute(
        """
        SELECT material_id,
               SUM(CASE WHEN destination = ? THEN qty ELSE 0 END)
             - SUM(CASE WHEN source = ? AND destination != 'consumed' THEN qty ELSE 0 END) AS qty
        FROM movements WHERE destination = ? OR source = ?
        GROUP BY material_id
        """,
        (location, location, location, location),
    ):
        if row["qty"] and row["qty"] > 1e-9:
            delivered[str(row["material_id"])] = row["qty"]
    return delivered


def _snags_by_house(db, include_created_at=False):
    grouped = {}
    for row in db.execute(
        "SELECT id, house_id, text, done, created_at FROM house_snags ORDER BY done, id"
    ):
        snag = {"id": row["id"], "text": row["text"], "done": bool(row["done"])}
        if include_created_at:
            snag["created_at"] = row["created_at"]
        grouped.setdefault(row["house_id"], []).append(snag)
    return grouped


def _validate_survey_payload(payload):
    if not isinstance(payload, dict):
        raise ApiError(400, "Survey data must be an object.")

    def dimensions(key):
        rows = payload.get(key)
        if not isinstance(rows, list) or len(rows) > 100:
            raise ApiError(400, f"{key.capitalize()} must contain at most 100 rows.")
        result = []
        for row in rows:
            if not isinstance(row, dict):
                raise ApiError(400, f"Invalid {key} measurement.")
            try:
                width, height = float(row.get("w")), float(row.get("h"))
                qty = row.get("qty", 1)
                if isinstance(qty, bool) or int(qty) != qty or not 1 <= int(qty) <= 100:
                    raise ValueError
                qty = int(qty)
            except (TypeError, ValueError, OverflowError):
                raise ApiError(400, f"Enter valid dimensions and quantity for {key}.")
            if not math.isfinite(width) or not math.isfinite(height) or not (0 < width <= 100 and 0 < height <= 100):
                raise ApiError(400, f"{key.capitalize()} dimensions must be between 0 and 100 metres.")
            result.append({"w": width, "h": height, "qty": qty})
        return result

    def lengths(key):
        rows = payload.get(key)
        if not isinstance(rows, list) or len(rows) > 200:
            raise ApiError(400, f"{key.capitalize()} must contain at most 200 lengths.")
        result = []
        for value in rows:
            try:
                length = float(value)
            except (TypeError, ValueError, OverflowError):
                raise ApiError(400, f"Enter a valid length for {key}.")
            if not math.isfinite(length) or not (0 < length <= 100):
                raise ApiError(400, f"{key.capitalize()} must be between 0 and 100 metres.")
            result.append(length)
        return result

    notes = str(payload.get("notes") or "").strip()
    if len(notes) > 2000:
        raise ApiError(400, "Survey notes must be 2,000 characters or fewer.")
    raw_perimeter = payload.get("perimeter_m")
    if raw_perimeter in (None, ""):
        perimeter = None
    else:
        try:
            perimeter = float(raw_perimeter)
        except (TypeError, ValueError, OverflowError):
            raise ApiError(400, "Enter a valid surveyed perimeter.")
        if not math.isfinite(perimeter) or not (0 < perimeter <= 10000):
            raise ApiError(400, "Surveyed perimeter must be between 0 and 10,000 metres.")
    survey = {
        "windows": dimensions("windows"),
        "doors": dimensions("doors"),
        "house_corners": lengths("house_corners"),
        "notes": notes,
    }
    if "perimeter_m" in payload:
        survey["perimeter_m"] = perimeter
    return survey


def _survey_has_valid_perimeter(survey):
    try:
        perimeter = float(survey.get("perimeter_m"))
    except (AttributeError, TypeError, ValueError, OverflowError):
        return False
    return math.isfinite(perimeter) and 0 < perimeter <= 10000


def _dashboard(db):
    snags = _snags_by_house(db)
    house_audit = _house_audit_by_id(db)
    materials = []
    for row in db.execute("SELECT * FROM materials ORDER BY name COLLATE NOCASE"):
        balances = balances_for(db, row["id"])
        warehouse = balances.get("warehouse")
        negative_locations = [
            location
            for location, quantity in balances.items()
            if location != "consumed" and quantity is not None and quantity < -1e-9
        ]
        materials.append(
            {
                "id": row["id"],
                "code": row["code"],
                "name": row["name"],
                "unit": row["unit"],
                "opening_qty": row["opening_qty"],
                "warehouse_qty": warehouse,
                "balances": balances,
                "negative_locations": negative_locations,
                "needs_stocktake": warehouse is None or bool(negative_locations),
            }
        )
    active_house_count = db.execute(
        """
        SELECT COUNT(*)
        FROM houses h
        LEFT JOIN provided_houses p ON p.id = h.id
        WHERE h.is_finished = 0 AND COALESCE(p.ready_for_start, 0) = 0
        """
    ).fetchone()[0]
    provided_house_count = db.execute(
        "SELECT COUNT(*) FROM provided_houses"
    ).fetchone()[0]
    closed_house_count = db.execute(
        """
        SELECT COUNT(*)
        FROM houses h
        LEFT JOIN provided_houses p ON p.id = h.id
        WHERE h.is_finished = 1 AND COALESCE(p.ready_for_start, 0) = 0
        """
    ).fetchone()[0]
    ready_house_count = db.execute(
        "SELECT COUNT(*) FROM provided_houses p WHERE p.ready_for_start = 1"
    ).fetchone()[0]
    not_started_house_count = db.execute(
        "SELECT COUNT(*) FROM provided_houses p WHERE p.ready_for_start = 0 "
        "AND NOT EXISTS (SELECT 1 FROM houses h WHERE h.id = p.id)"
    ).fetchone()[0]
    team_house_counts = {}
    for row in db.execute(
        """
        SELECT DISTINCT m.team, m.department, h.id, h.is_finished,
               COALESCE(p.ready_for_start, 0) AS ready_for_start,
               h.status_changed_at
        FROM movements m
        JOIN houses h
          ON h.id = m.house_id
          OR m.source = 'house:' || h.id
          OR m.destination = 'house:' || h.id
        LEFT JOIN provided_houses p ON p.id = h.id
        WHERE m.team != ''
        ORDER BY h.status_changed_at DESC, h.id COLLATE NOCASE
        """
    ):
        if row["ready_for_start"]:
            continue
        counts = team_house_counts.setdefault(
            team_location(row["team"], row["department"]),
            {"open_house_ids": [], "closed_house_ids": []},
        )
        house_ids = (
            counts["closed_house_ids"]
            if row["is_finished"]
            else counts["open_house_ids"]
        )
        house_ids.append(row["id"])
    for row in db.execute(
        """
        SELECT h.id, h.employee_name, h.is_finished,
               COALESCE(p.ready_for_start, 0) AS ready_for_start
        FROM houses h
        LEFT JOIN provided_houses p ON p.id = h.id
        WHERE TRIM(h.employee_name) != ''
        """
    ):
        if row["ready_for_start"]:
            continue
        counts = team_house_counts.setdefault(
            employee_location(row["employee_name"]),
            {"open_house_ids": [], "closed_house_ids": []},
        )
        house_ids = (
            counts["closed_house_ids"]
            if row["is_finished"]
            else counts["open_house_ids"]
        )
        house_ids.append(row["id"])
    for counts in team_house_counts.values():
        counts["open_house_ids"].sort(key=str.casefold)
        counts["closed_house_ids"].sort(key=str.casefold)
        counts["open"] = len(counts["open_house_ids"])
        counts["closed"] = len(counts["closed_house_ids"])
    return {
        "materials": materials,
        "team_rosters": _team_rosters(db),
        "demo_mode": (
            db.execute(
                "SELECT value FROM app_settings WHERE key = 'demo_mode'"
            ).fetchone() or {"value": "0"}
        )["value"] == "1",
        "houses": active_house_count,
        "house_summary": {
            "provided": provided_house_count,
            "active": active_house_count,
            "closed": closed_house_count,
            "ready": ready_house_count,
            "not_started": not_started_house_count,
        },
        "movements": [_movement_json(row) for row in _movement_rows(db, 12)],
        "teams": sorted(
            {
                " · ".join(filter(None, (row["department"], row["team"])))
                for row in db.execute(
                    "SELECT DISTINCT department, team FROM movements WHERE team != ''"
                )
            },
            key=str.casefold,
        ),
        "team_house_counts": team_house_counts,
        "house_list": _attach_seai_application_ids(db, [
            {
                **dict(row),
                "esb_unclipped": _esb_unclipped_status(row["esb_unclipped"]),
                "snags": snags.get(row["id"], []),
                **house_audit.get(
                    row["id"], {"status_history": [], "last_movement": None}
                ),
            }
            for row in db.execute(
                """
                SELECT h.id, COALESCE(NULLIF(p.postcode, ''), h.postcode) AS postcode,
                       COALESCE(NULLIF(p.owner_name, ''), h.owner_name) AS owner_name,
                       h.employee_name, h.delivery_person, h.delivery_team,
                       ewi_assignment.teamlead_user_id AS ewi_teamlead_user_id,
                       COALESCE(ewi_teamlead.display_name, '') AS ewi_teamlead_name,
                       COALESCE(ewi_assignment.assigned_by, '') AS ewi_assigned_by,
                       COALESCE(ewi_assignment.assigned_at, '') AS ewi_assigned_at,
                       COALESCE(ewi_assignment.completed_by, '') AS ewi_completed_by,
                       COALESCE(ewi_assignment.completed_at, '') AS ewi_completed_at,
                       p.address, p.source, p.area_m2, p.perimeter_m,
                       p.latitude, p.longitude, h.is_finished,
                       COALESCE(p.ready_for_start, 0) AS ready_for_start,
                       h.stage, h.status_changed_at, COALESCE(p.notes, '') AS notes,
                       COALESCE(hs.updated_by, '') AS survey_updated_by,
                       COALESCE(hs.updated_at, '') AS survey_updated_at,
                       hs.data AS survey_data,
                       COALESCE(hs.notes, '') AS survey_notes,
                       p.plan_scaffolding_required,
                       COALESCE(p.plan_scaffolding_notes, '') AS plan_scaffolding_notes,
                       COALESCE(p.plan_scaffolding_done_by, '') AS plan_scaffolding_done_by,
                       COALESCE(p.plan_scaffolding_done_at, '') AS plan_scaffolding_done_at,
                       p.plan_scaffolding_done_by_user_id,
                       p.plan_esb_required,
                       COALESCE(p.plan_esb_notes, '') AS plan_esb_notes,
                       COALESCE(p.plan_esb_done_by, '') AS plan_esb_done_by,
                       COALESCE(p.plan_esb_done_at, '') AS plan_esb_done_at,
                       p.plan_esb_done_by_user_id,
                       p.plan_plumber_required,
                       COALESCE(p.plan_plumber_notes, '') AS plan_plumber_notes,
                       COALESCE(p.plan_plumber_done_by, '') AS plan_plumber_done_by,
                       COALESCE(p.plan_plumber_done_at, '') AS plan_plumber_done_at,
                       p.plan_plumber_done_by_user_id,
                       p.plan_vent_required,
                       COALESCE(p.plan_vent_notes, '') AS plan_vent_notes,
                       COALESCE(p.plan_vent_done_by, '') AS plan_vent_done_by,
                       COALESCE(p.plan_vent_done_at, '') AS plan_vent_done_at,
                       p.plan_vent_done_by_user_id,
                       p.plan_ceramic_fuse_board,
                       p.plan_ceramic_fuse_board_done_by,
                       p.plan_ceramic_fuse_board_done_at,
                       p.plan_ceramic_fuse_board_done_by_user_id,
                       p.plan_solar_pv_required,
                       p.plan_solar_pv_required_done_by,
                       p.plan_solar_pv_required_done_at,
                       p.plan_solar_pv_required_done_by_user_id,
                       p.plan_pir_required,
                       p.plan_pir_required_done_by,
                       p.plan_pir_required_done_at,
                       p.plan_pir_required_done_by_user_id,
                       p.plan_order_variations,
                       p.plan_order_variations_done_by,
                       p.plan_order_variations_done_at,
                       p.plan_order_variations_done_by_user_id,
                       p.plan_windows_required,
                       p.plan_windows_required_done_by,
                       p.plan_windows_required_done_at,
                       p.plan_windows_required_done_by_user_id,
                       assigned_surveyor.display_name AS assigned_surveyor_name,
                       assigned_surveyor.is_active AS assigned_surveyor_is_active,
                       surveyor_assignment.assigned_by AS surveyor_assigned_by,
                       surveyor_assignment.assigned_at AS surveyor_assigned_at,
                       EXISTS (
                           SELECT 1
                           FROM house_role_assignments assignment
                           JOIN app_users assigned_user
                             ON assigned_user.id = assignment.user_id
                           WHERE assignment.house_id = h.id
                             AND assignment.role = 'surveyor'
                             AND assigned_user.is_active = 1
                             AND EXISTS (
                                 SELECT 1 FROM app_role_permissions access
                                 WHERE access.role_key = assigned_user.role
                                   AND access.permission_key = 'access.surveyor'
                                   AND access.is_allowed = 1
                             )
                       ) AS has_assigned_surveyor,
                       c.scaffolding_status, c.esb_unclipped,
                       CASE WHEN COALESCE(p.plan_plumber_done_at, '') != ''
                            THEN 'done'
                            ELSE c.plumber_status
                       END AS plumber_status
                FROM houses h
                LEFT JOIN house_checklists c ON c.house_id = h.id
                LEFT JOIN provided_houses p ON p.id = h.id
                LEFT JOIN house_surveys hs ON hs.house_id = h.id
                LEFT JOIN house_role_assignments surveyor_assignment
                  ON surveyor_assignment.house_id = h.id
                 AND surveyor_assignment.role = 'surveyor'
                LEFT JOIN app_users assigned_surveyor
                  ON assigned_surveyor.id = surveyor_assignment.user_id
                LEFT JOIN house_ewi_assignments ewi_assignment
                  ON ewi_assignment.house_id = h.id
                LEFT JOIN app_users ewi_teamlead
                  ON ewi_teamlead.id = ewi_assignment.teamlead_user_id
                ORDER BY h.status_changed_at DESC, h.id COLLATE NOCASE
                """
            )
        ]),
        "locations": sorted(known_locations(db)),
    }


def _is_vent_photo_uploader(user):
    return (
        user is not None
        and (user["username"] or "").strip().casefold() == "krystian.stelmach"
    )


def _can_view_vent_house_photos(user):
    return (
        _is_vent_photo_uploader(user)
        or _has_access_profile(user, "vent_photos")
        or _has_access_profile(user, "admin")
        or _has_access_profile(user, "ventilation_coordinator")
    )


def _vent_house_photo_metadata(db, house_id):
    return [
        {
            "id": row["id"],
            "filename": row["filename"],
            "size_bytes": row["size_bytes"],
            "uploaded_by": row["uploaded_by"],
            "uploaded_at": row["uploaded_at"],
        }
        for row in db.execute(
            """
            SELECT id, filename, size_bytes, uploaded_by, uploaded_at
            FROM house_vent_photos
            WHERE house_id = ?
            ORDER BY id DESC
            """,
            (house_id,),
        )
    ]


def _plumbing_house_photo_metadata(db, house_id):
    return [
        {
            "id": row["id"],
            "house_id": house_id,
            "filename": row["filename"],
            "size_bytes": row["size_bytes"],
            "uploaded_by": row["uploaded_by"],
            "uploaded_at": row["uploaded_at"],
            "activity": row["activity"],
        }
        for row in db.execute(
            """
            SELECT id, filename, size_bytes, uploaded_by, uploaded_at, activity
            FROM house_plumbing_photos
            WHERE house_id = ?
            ORDER BY id DESC
            """,
            (house_id,),
        )
    ]


def _has_plumbing_photo_view_access(db, user):
    return _has_access_profile(user, "admin") or db.execute(
        "SELECT 1 FROM app_user_plumbing_photo_access WHERE user_id = ?",
        (user["id"],),
    ).fetchone() is not None


def _can_view_plumbing_house_photos(db, user):
    return (
        _has_plumbing_photo_view_access(db, user)
        or _is_plumbing_supervisor(db, user)
    )


def _ventilation_house_list(db, teamlead_user_id, include_photos=False):
    houses = [
        {
            "id": row["id"],
            "postcode": row["postcode"],
            "address": row["address"],
            "owner_name": row["owner_name"],
            "status": row["status"],
            "notes": row["notes"],
            "done_by": row["plan_vent_done_by"],
            "done_at": row["plan_vent_done_at"],
            "assigned_teamlead_user_id": row["assigned_teamlead_user_id"],
            "assigned_teamlead_name": row["assigned_teamlead_name"],
            "assigned_team_name": row["assigned_team_name"],
            "assignment_assigned_by": row["assignment_assigned_by"],
            "documents": [],
            "survey_documents": [],
            "vent_photos": [],
            "can_view_vent_photos": bool(include_photos),
        }
        for row in db.execute(
            """
            SELECT p.id, p.postcode, p.address, p.owner_name,
                   COALESCE(p.plan_vent_notes, '') AS notes,
                   COALESCE(p.plan_vent_done_by, '') AS plan_vent_done_by,
                   COALESCE(p.plan_vent_done_at, '') AS plan_vent_done_at,
                   assignment.teamlead_user_id AS assigned_teamlead_user_id,
                   COALESCE(teamlead.display_name, '') AS assigned_teamlead_name,
                   COALESCE(teamlead.team_name, '') AS assigned_team_name,
                   COALESCE(assignment.assigned_by, '') AS assignment_assigned_by,
                   CASE WHEN p.ready_for_start = 1 THEN 'ready'
                        WHEN h.id IS NULL THEN 'not_started'
                        WHEN h.is_finished = 1 THEN 'closed'
                        ELSE 'active' END AS status
            FROM provided_houses p
            LEFT JOIN houses h ON h.id = p.id
            LEFT JOIN house_vent_assignments assignment ON assignment.house_id = p.id
            LEFT JOIN app_users teamlead ON teamlead.id = assignment.teamlead_user_id
            WHERE p.plan_vent_required = 1
              AND assignment.teamlead_user_id = ?
              AND NOT EXISTS (
                  SELECT 1
                  FROM house_status_history reset
                  WHERE reset.house_id = p.id
                    AND reset.status = 'not_started'
                    AND reset.id = (
                        SELECT latest.id
                        FROM house_status_history latest
                        WHERE latest.house_id = p.id
                          AND latest.status = 'not_started'
                        ORDER BY latest.changed_at DESC, latest.id DESC
                        LIMIT 1
                    )
                    AND assignment.assigned_at < reset.changed_at
              )
            ORDER BY
                (COALESCE(p.plan_vent_done_at, '') = ''),
                COALESCE(p.plan_vent_done_at, '') DESC,
                p.id COLLATE NOCASE
            """,
            (teamlead_user_id,),
        )
    ]
    documents_by_house = {}
    for row in db.execute(
        """
        SELECT documents.id, documents.house_id, documents.filename,
               documents.size_bytes, documents.shared_by, documents.shared_at,
               (
                   (
                       annotations.source_document_id IS NOT NULL
                       OR annotations.source_seai_document_id IS NOT NULL
                   )
                   AND annotations.annotations_json IS NOT NULL
               ) AS annotations_editable
        FROM house_vent_documents documents
        LEFT JOIN house_vent_document_annotations annotations
          ON annotations.annotated_document_id = documents.id
        JOIN provided_houses houses ON houses.id = documents.house_id
        JOIN house_vent_assignments assignment
          ON assignment.house_id = houses.id
        WHERE houses.plan_vent_required = 1
          AND assignment.teamlead_user_id = ?
          AND NOT EXISTS (
              SELECT 1
              FROM house_status_history reset
              WHERE reset.house_id = houses.id
                AND reset.status = 'not_started'
                AND reset.id = (
                    SELECT latest.id
                    FROM house_status_history latest
                    WHERE latest.house_id = houses.id
                      AND latest.status = 'not_started'
                    ORDER BY latest.changed_at DESC, latest.id DESC
                    LIMIT 1
                )
                AND assignment.assigned_at < reset.changed_at
          )
        ORDER BY documents.id DESC
        """,
        (teamlead_user_id,),
    ):
        documents_by_house.setdefault(row["house_id"], []).append(
            {
                key: row[key]
                for key in (
                    "id",
                    "filename",
                    "size_bytes",
                    "shared_by",
                    "shared_at",
                    "annotations_editable",
                )
            }
        )
    for house in houses:
        house["documents"] = documents_by_house.get(house["id"], [])
    if include_photos and houses:
        house_ids = [house["id"] for house in houses]
        placeholders = ",".join("?" for _ in house_ids)
        photos_by_house = {}
        for row in db.execute(
            f"""
            SELECT id, house_id, filename, size_bytes, uploaded_by, uploaded_at
            FROM house_vent_photos
            WHERE house_id IN ({placeholders})
            ORDER BY id DESC
            """,
            house_ids,
        ):
            photos_by_house.setdefault(row["house_id"], []).append(
                {
                    "id": row["id"],
                    "filename": row["filename"],
                    "size_bytes": row["size_bytes"],
                    "uploaded_by": row["uploaded_by"],
                    "uploaded_at": row["uploaded_at"],
                }
            )
        for house in houses:
            house["vent_photos"] = photos_by_house.get(house["id"], [])
    survey_documents_by_house = {}
    if houses:
        house_ids = [house["id"] for house in houses]
        placeholders = ",".join("?" for _ in house_ids)
        has_seai_pdf_storage = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'seai_documents'"
        ).fetchone()
        if has_seai_pdf_storage:
            seai_columns = {
                row["name"] for row in db.execute("PRAGMA table_info(seai_documents)")
            }
            if "content" in seai_columns:
                for row in db.execute(
                    f"""
                    SELECT selected.house_id, documents.id,
                           documents.filename, length(documents.content) AS size_bytes,
                           selected.created_by AS saved_by,
                           selected.created_at AS saved_at
                    FROM selected_seai_documents selected
                    JOIN seai_documents documents ON documents.id = selected.document_id
                    JOIN provided_houses houses ON houses.id = selected.house_id
                    JOIN house_vent_assignments assignment
                      ON assignment.house_id = houses.id
                    WHERE selected.house_id IN ({placeholders})
                      AND lower(documents.filename) LIKE '%.pdf'
                      AND documents.content IS NOT NULL
                      AND selected.created_by != ''
                      AND houses.plan_vent_required = 1
                      AND assignment.teamlead_user_id = ?
                      AND NOT EXISTS (
                          SELECT 1
                          FROM house_status_history reset
                          WHERE reset.house_id = houses.id
                            AND reset.status = 'not_started'
                            AND reset.id = (
                                SELECT latest.id
                                FROM house_status_history latest
                                WHERE latest.house_id = houses.id
                                  AND latest.status = 'not_started'
                                ORDER BY latest.changed_at DESC, latest.id DESC
                                LIMIT 1
                            )
                            AND assignment.assigned_at < reset.changed_at
                      )
                    ORDER BY documents.id DESC
                    """,
                    (*house_ids, teamlead_user_id),
                ):
                    survey_documents_by_house.setdefault(row["house_id"], []).append(
                        {
                            "id": row["id"],
                            "filename": row["filename"],
                            "size_bytes": row["size_bytes"],
                            "uploaded_by": row["saved_by"] or "Surveyor",
                            "uploaded_at": row["saved_at"] or "",
                            "document_type": "Combined PDF by Surveyor",
                        }
                    )
    for house in houses:
        house["survey_documents"] = survey_documents_by_house.get(house["id"], [])
    return _attach_seai_application_ids(db, houses)


def _ventilation_house_is_visible(db, house_id, teamlead_user_id):
    return any(
        house["id"] == house_id
        for house in _ventilation_house_list(db, teamlead_user_id)
    )


def _ventilation_coordinator_house_is_visible(db, house_id):
    return any(
        house["id"] == house_id
        for house in _ventilation_coordinator_dashboard(db)[
            "ventilation_coordinator_houses"
        ]
    )


def _plumbing_coordinator_dashboard(db):
    supervisors = [
        {"id": row["id"], "name": row["display_name"]}
        for row in db.execute(
            """
            SELECT users.id, users.display_name
            FROM app_users users
            WHERE users.is_active = 1
              AND EXISTS (
                  SELECT 1 FROM app_role_permissions access
                  WHERE access.role_key = users.role
                    AND access.permission_key = 'access.supervisor'
                    AND access.is_allowed = 1
              )
              AND (
                  users.supervisor_specialty = 'Plumbing' COLLATE NOCASE
                  OR EXISTS (
                      SELECT 1 FROM user_departments memberships
                      JOIN departments ON departments.id = memberships.department_id
                      WHERE memberships.user_id = users.id
                        AND departments.name = 'Plumbing' COLLATE NOCASE
                        AND departments.is_active = 1
                  )
              )
            ORDER BY users.display_name COLLATE NOCASE
            """
        )
    ]
    houses = [
        dict(row)
        for row in db.execute(
            """
            SELECT p.id, p.postcode, p.address, p.owner_name,
                   COALESCE(p.plan_plumber_notes, '') AS notes,
                   COALESCE(hc.plumber_status, p.plan_plumber, 'not_done')
                       AS plumber_status,
                   COALESCE(surveyor.display_name, '') AS surveyor_name,
                   COALESCE(p.plan_plumber_done_by, '') AS plumber_done_by,
                   COALESCE(p.plan_plumber_done_at, '') AS plumber_done_at,
                   p.plan_plumber_done_by_user_id,
                   CASE WHEN p.ready_for_start = 1 THEN 'ready'
                        WHEN h.id IS NULL THEN 'not_started'
                        WHEN h.is_finished = 1 THEN 'closed'
                        ELSE 'active' END AS status,
                   assigned_user.id AS supervisor_user_id,
                   assigned_user.display_name AS supervisor_name,
                   CASE WHEN assigned_user.id IS NOT NULL
                        THEN COALESCE(
                            assignment.assigned_by,
                            legacy_supervisor_assignment.assigned_by,
                            ''
                        ) ELSE '' END
                       AS assignment_assigned_by,
                   CASE WHEN assigned_user.id IS NOT NULL
                        THEN COALESCE(
                            assignment.assigned_at,
                            legacy_supervisor_assignment.assigned_at,
                            ''
                        ) ELSE '' END
                       AS assignment_assigned_at
            FROM provided_houses p
            LEFT JOIN houses h ON h.id = p.id
            LEFT JOIN house_checklists hc ON hc.house_id = p.id
            LEFT JOIN house_role_assignments survey_assignment
              ON survey_assignment.house_id = p.id
             AND survey_assignment.role = 'surveyor'
            LEFT JOIN app_users surveyor
              ON surveyor.id = survey_assignment.user_id
            LEFT JOIN house_plumbing_assignments assignment
              ON assignment.house_id = p.id
            LEFT JOIN house_role_assignments legacy_supervisor_assignment
              ON legacy_supervisor_assignment.house_id = p.id
             AND legacy_supervisor_assignment.role = 'supervisor'
            LEFT JOIN app_users assigned_user
              ON assigned_user.id = COALESCE(
                  assignment.supervisor_user_id,
                  legacy_supervisor_assignment.user_id
              )
             AND (
                 assigned_user.supervisor_specialty = 'Plumbing' COLLATE NOCASE
                 OR EXISTS (
                     SELECT 1 FROM user_departments memberships
                     JOIN departments ON departments.id = memberships.department_id
                     WHERE memberships.user_id = assigned_user.id
                       AND departments.name = 'Plumbing' COLLATE NOCASE
                       AND departments.is_active = 1
                 )
             )
            WHERE p.plan_plumber_required = 1
              AND COALESCE(hc.plumber_status, p.plan_plumber, 'not_done')
                    != 'not_necessary'
              AND (
                  h.id IS NULL
                  OR h.is_finished = 0
                  OR COALESCE(hc.plumber_status, p.plan_plumber, 'not_done') = 'done'
              )
            ORDER BY
                CASE WHEN COALESCE(hc.plumber_status, p.plan_plumber, 'not_done') = 'done'
                     THEN 1 ELSE 0 END,
                CASE WHEN h.id IS NOT NULL THEN 0
                     WHEN p.ready_for_start = 1 THEN 1 ELSE 2 END,
                p.id COLLATE NOCASE
            """
        )
    ]
    return {
        "plumbing_houses": _attach_seai_application_ids(db, houses),
        "plumbing_supervisors": supervisors,
    }


def _plumbing_coordinator_house_is_visible(db, house_id):
    return db.execute(
        """
        SELECT 1
        FROM provided_houses p
        LEFT JOIN houses h ON h.id = p.id
        LEFT JOIN house_checklists hc ON hc.house_id = p.id
        WHERE p.id = ?
          AND p.plan_plumber_required = 1
          AND COALESCE(hc.plumber_status, p.plan_plumber, 'not_done')
                != 'not_necessary'
          AND (
              h.id IS NULL
              OR h.is_finished = 0
              OR COALESCE(hc.plumber_status, p.plan_plumber, 'not_done') = 'done'
          )
        """,
        (house_id,),
    ).fetchone() is not None


def _ventilation_coordinator_dashboard(db):
    teamleads = [
        {"id": row["id"], "name": row["display_name"], "team_name": row["team_name"]}
        for row in db.execute(
            """
            SELECT users.id, users.display_name, users.team_name
            FROM app_users users
            WHERE users.is_active = 1
              AND EXISTS (
                  SELECT 1 FROM app_role_permissions access
                  WHERE access.role_key = users.role
                    AND access.permission_key = 'access.teamlead'
                    AND access.is_allowed = 1
              )
              AND EXISTS (
                  SELECT 1 FROM user_departments memberships
                  JOIN departments ON departments.id = memberships.department_id
                  WHERE memberships.user_id = users.id
                    AND departments.name = 'Ventilation' COLLATE NOCASE
                    AND departments.is_active = 1
              )
            ORDER BY users.display_name COLLATE NOCASE
            """
        )
    ]
    insulation_teamleads = [
        {"id": row["id"], "name": row["display_name"], "team_name": row["team_name"]}
        for row in db.execute(
            """
            SELECT users.id, users.display_name, users.team_name
            FROM app_users users
            WHERE users.is_active = 1
              AND EXISTS (
                  SELECT 1 FROM app_role_permissions access
                  WHERE access.role_key = users.role
                    AND access.permission_key = 'access.teamlead'
                    AND access.is_allowed = 1
              )
            ORDER BY users.display_name COLLATE NOCASE
            """
        )
    ]
    houses = [
        dict(row)
        for row in db.execute(
            """
            SELECT p.id, p.postcode, p.address, p.owner_name,
                   COALESCE(p.plan_vent_notes, '') AS notes,
                   COALESCE(p.plan_vent_done_by, '') AS done_by,
                   COALESCE(p.plan_vent_done_at, '') AS done_at,
                   p.plan_vent_required,
                   'ready' AS status,
                   assignment.teamlead_user_id AS assigned_teamlead_user_id,
                   COALESCE(teamlead.display_name, '') AS assigned_teamlead_name,
                   COALESCE(teamlead.team_name, '') AS assigned_team_name,
                   COALESCE(assignment.assigned_by, '') AS assignment_assigned_by,
                   COALESCE(assignment.assigned_at, '') AS assignment_assigned_at,
                   COALESCE(surveyor.display_name, '') AS surveyor_name,
                   survey.data AS survey_data,
                   COALESCE(survey.notes, '') AS survey_notes,
                   COALESCE(survey.updated_by, '') AS survey_updated_by,
                   COALESCE(survey.updated_at, '') AS survey_updated_at
            FROM provided_houses p
            JOIN house_surveys survey ON survey.house_id = p.id
            LEFT JOIN house_vent_assignments assignment ON assignment.house_id = p.id
            LEFT JOIN app_users teamlead ON teamlead.id = assignment.teamlead_user_id
            LEFT JOIN house_role_assignments survey_assignment
              ON survey_assignment.house_id = p.id
             AND survey_assignment.role = 'surveyor'
            LEFT JOIN app_users surveyor
              ON surveyor.id = survey_assignment.user_id
            WHERE p.ready_for_start = 1
            ORDER BY p.id COLLATE NOCASE
            """
        )
    ]
    quantities = _attic_cavity_quantities(db, [house["id"] for house in houses])
    assignment_rows = [
        dict(row)
        for row in db.execute(
            """
            SELECT assignment.house_id, assignment.work_type,
                   assignment.teamlead_user_id, teamlead.display_name AS teamlead_name,
                   teamlead.team_name, assignment.assigned_by, assignment.assigned_at,
                   assignment.completed_by, assignment.completed_at
            FROM house_insulation_assignments assignment
            JOIN app_users teamlead ON teamlead.id = assignment.teamlead_user_id
            """
        )
    ]
    assignments = {}
    for assignment in assignment_rows:
        assignments.setdefault(assignment["house_id"], {})[
            assignment["work_type"]
        ] = assignment
    eligible_houses = []
    for house in houses:
        required = quantities.get(house["id"], {})
        if house["plan_vent_required"] != 1 and not required:
            continue
        try:
            survey_data = json.loads(house.pop("survey_data") or "{}")
        except (TypeError, ValueError) as exc:
            raise ApiError(500, f"Saved survey for {house['id']} is invalid.") from exc
        if not isinstance(survey_data, dict) or not _survey_has_valid_perimeter(
            survey_data
        ):
            continue
        house["survey_data"] = survey_data
        house["attic_required"] = "attic" in required
        house["attic_survey_quantity"] = required.get("attic", "")
        house["cavity_required"] = "cavity" in required
        house["cavity_survey_quantity"] = required.get("cavity", "")
        for work_type in ("attic", "cavity"):
            assignment = assignments.get(house["id"], {}).get(work_type, {})
            prefix = f"{work_type}_assignment"
            for key in (
                "teamlead_user_id",
                "teamlead_name",
                "team_name",
                "assigned_by",
                "assigned_at",
                "completed_by",
                "completed_at",
            ):
                house[f"{prefix}_{key}"] = assignment.get(key)
        eligible_houses.append(house)
    return {
        "ventilation_coordinator_houses": _attach_seai_application_ids(
            db, eligible_houses
        ),
        "ventilation_teamleads": teamleads,
        "insulation_teamleads": insulation_teamleads,
    }


def _team_rosters(db):
    rosters = {}
    for row in db.execute(
        """
        SELECT users.id AS teamlead_id, users.display_name AS teamlead_name,
               users.team_name,
               members.id AS member_id, members.member_name
        FROM app_users users
        LEFT JOIN team_roster_members members
          ON members.teamlead_user_id = users.id
        WHERE users.is_active = 1
          AND EXISTS (
              SELECT 1 FROM app_role_permissions access
              WHERE access.role_key = users.role
                AND access.is_allowed = 1
                AND access.permission_key = 'access.teamlead'
          )
        ORDER BY users.display_name COLLATE NOCASE,
                 members.member_name COLLATE NOCASE
        """
    ):
        roster = rosters.setdefault(
            row["teamlead_id"],
            {
                "teamlead_id": row["teamlead_id"],
                "teamlead_name": row["teamlead_name"],
                "team_name": row["team_name"],
                "members": [],
            },
        )
        if row["member_id"] is not None:
            roster["members"].append(
                {"id": row["member_id"], "name": row["member_name"]}
            )
    return list(rosters.values())


def _visible_team_rosters(db, user):
    rosters = _team_rosters(db)
    if _has_access_profile(user, "admin"):
        return rosters
    if _has_access_profile(user, "teamlead"):
        visible_ids = {user["id"]}
    elif _has_access_profile(user, "supervisor"):
        visible_ids = {
            row["report_user_id"]
            for row in db.execute(
                """
                SELECT lines.report_user_id
                FROM user_reporting_lines lines
                JOIN app_users reports ON reports.id = lines.report_user_id
                JOIN app_role_permissions access ON access.role_key = reports.role
                WHERE lines.manager_user_id = ?
                  AND reports.is_active = 1
                  AND access.permission_key = 'access.teamlead'
                  AND access.is_allowed = 1
                """,
                (user["id"],),
            )
        }
    else:
        visible_ids = set()
    return [roster for roster in rosters if roster["teamlead_id"] in visible_ids]


def _surveyor_workload_summary(db):
    surveyors = {}
    rows = db.execute(
        """
        SELECT u.id AS user_id, u.display_name, p.id AS house_id,
               hs.data AS survey_data
        FROM app_users u
        LEFT JOIN house_role_assignments assignment
          ON assignment.user_id = u.id AND assignment.role = 'surveyor'
        LEFT JOIN provided_houses p
          ON p.id = assignment.house_id
         AND p.ready_for_start = 1
         AND NOT EXISTS (SELECT 1 FROM houses h WHERE h.id = p.id)
        LEFT JOIN house_surveys hs ON hs.house_id = p.id
        WHERE u.is_active = 1
          AND EXISTS (
              SELECT 1 FROM app_role_permissions access
              WHERE access.role_key = u.role
                AND access.is_allowed = 1
                AND access.permission_key = 'access.surveyor'
          )
        ORDER BY u.display_name COLLATE NOCASE, p.id COLLATE NOCASE
        """
    )
    for row in rows:
        surveyor = surveyors.setdefault(
            row["user_id"],
            {
                "user_id": row["user_id"],
                "name": row["display_name"],
                "pending_house_ids": [],
                "completed_count": 0,
            },
        )
        if row["house_id"] is None:
            continue
        survey_data = json.loads(row["survey_data"]) if row["survey_data"] else {}
        if _survey_has_valid_perimeter(survey_data):
            surveyor["completed_count"] += 1
        else:
            surveyor["pending_house_ids"].append(row["house_id"])
    result = []
    for surveyor in surveyors.values():
        surveyor["pending_count"] = len(surveyor["pending_house_ids"])
        surveyor["busy"] = surveyor["pending_count"] > 0
        result.append(surveyor)
    return result


def _import_house_locations_csv(db, contents):
    try:
        reader = csv.DictReader(StringIO(contents.decode("utf-8-sig")), strict=True)
    except UnicodeDecodeError:
        raise ApiError(400, "House-location CSV must use UTF-8 encoding.")
    if not reader.fieldnames:
        raise ApiError(400, "House-location CSV needs a header row.")
    try:
        records = list(reader)
    except csv.Error:
        raise ApiError(400, "House-location CSV is malformed.")

    aliases = {
        "id": {"id", "houseid", "house"},
        "postcode": {"postcode", "eircode", "irishpostcode"},
        "address": {"address", "propertyaddress"},
        "owner_name": {"owner", "ownername"},
        "area_m2": {"area", "aream2", "floorarea"},
        "perimeter_m": {"perimeter", "perimeterm"},
        "latitude": {"latitude", "lat"},
        "longitude": {"longitude", "lon", "lng", "long"},
    }
    columns = {}
    for raw_name in reader.fieldnames:
        normalized = re.sub(r"[^a-z0-9]", "", str(raw_name or "").casefold())
        for name, accepted in aliases.items():
            if normalized in accepted:
                if name in columns:
                    raise ApiError(400, f"House-location CSV has duplicate {name} columns.")
                columns[name] = raw_name
                break
    required = {"id", "postcode", "latitude", "longitude"}
    if not required.issubset(columns):
        missing = ", ".join(sorted(required - columns.keys()))
        raise ApiError(400, f"House-location CSV is missing required columns: {missing}.")

    rows = []
    house_ids = set()

    def optional_number(raw, label, row_number):
        if raw is None or not str(raw).strip():
            return None
        try:
            value = float(raw)
        except (TypeError, ValueError):
            raise ApiError(400, f"House-location CSV row {row_number} has invalid {label}.")
        if not math.isfinite(value) or value < 0:
            raise ApiError(400, f"House-location CSV row {row_number} has invalid {label}.")
        return value

    if len(records) > 10000:
        raise ApiError(400, "House-location CSV cannot contain more than 10,000 rows.")
    for row_number, record in enumerate(records, start=2):
        house_id = str(record.get(columns["id"]) or "").strip()
        postcode = str(record.get(columns["postcode"]) or "").strip().upper()
        if not house_id or len(house_id) > 100:
            raise ApiError(400, f"House-location CSV row {row_number} needs a valid house ID.")
        if not postcode:
            raise ApiError(400, f"House-location CSV row {row_number} needs an Eircode.")
        if house_id.casefold() in house_ids:
            raise ApiError(400, f"House-location CSV contains duplicate house ID {house_id}.")
        house_ids.add(house_id.casefold())
        try:
            latitude = float(record.get(columns["latitude"], ""))
            longitude = float(record.get(columns["longitude"], ""))
        except (TypeError, ValueError):
            raise ApiError(400, f"House-location CSV row {row_number} needs valid coordinates.")
        if not math.isfinite(latitude) or not -90 <= latitude <= 90:
            raise ApiError(400, f"House-location CSV row {row_number} has invalid latitude.")
        if not math.isfinite(longitude) or not -180 <= longitude <= 180:
            raise ApiError(400, f"House-location CSV row {row_number} has invalid longitude.")
        rows.append(
            {
                "id": house_id,
                "postcode": postcode,
                "address": str(record.get(columns.get("address", "")) or "").strip(),
                "owner_name": str(record.get(columns.get("owner_name", "")) or "").strip(),
                "area_m2": optional_number(
                    record.get(columns.get("area_m2", "")), "area", row_number
                ),
                "perimeter_m": optional_number(
                    record.get(columns.get("perimeter_m", "")), "perimeter", row_number
                ),
                "latitude": latitude,
                "longitude": longitude,
            }
        )
    if not rows:
        raise ApiError(400, "House-location CSV contains no house records.")

    updated = 0
    for house in rows:
        exists = db.execute(
            "SELECT 1 FROM provided_houses WHERE id = ?", (house["id"],)
        ).fetchone()
        updated += bool(exists)
        db.execute(
            """
            INSERT INTO provided_houses
                (id, postcode, address, owner_name, area_m2, perimeter_m,
                 latitude, longitude, source)
            VALUES
                (:id, :postcode, :address, :owner_name, :area_m2, :perimeter_m,
                 :latitude, :longitude, 'government')
            ON CONFLICT(id) DO UPDATE SET
                postcode = excluded.postcode,
                address = COALESCE(NULLIF(excluded.address, ''), provided_houses.address),
                owner_name = COALESCE(NULLIF(excluded.owner_name, ''), provided_houses.owner_name),
                area_m2 = COALESCE(excluded.area_m2, provided_houses.area_m2),
                perimeter_m = COALESCE(excluded.perimeter_m, provided_houses.perimeter_m),
                latitude = excluded.latitude,
                longitude = excluded.longitude,
                source = 'government'
            """,
            house,
        )
    total = db.execute("SELECT COUNT(*) FROM provided_houses").fetchone()[0]
    return {"imported": len(rows) - updated, "updated": updated, "total": total}


def _import_workbook(db, path):
    try:
        from openpyxl import load_workbook
        from openpyxl.utils.exceptions import InvalidFileException
    except ImportError:
        raise ApiError(501, "Excel import requires openpyxl. Install dependencies from requirements.txt.")

    try:
        workbook = load_workbook(path, data_only=True, read_only=True)
    except (OSError, ValueError, zipfile.BadZipFile, ParseError, InvalidFileException) as exc:
        raise ApiError(400, f"Could not read this Excel workbook: {exc}")
    try:
        return _import_workbook_contents(db, workbook)
    finally:
        workbook.close()


def _import_workbook_contents(db, workbook):
    if "STOCK" not in workbook.sheetnames or "MOVEMENTS" not in workbook.sheetnames:
        raise ApiError(400, "Workbook must contain STOCK and MOVEMENTS sheets.")
    if db.execute("SELECT 1 FROM materials LIMIT 1").fetchone():
        raise ApiError(409, "Import is only allowed into an empty tracker to prevent duplicate records.")

    stock = workbook["STOCK"]
    movement_sheet = workbook["MOVEMENTS"]
    stock_headers = {
        str(stock.cell(1, column).value or "").strip().casefold(): column
        for column in range(1, stock.max_column + 1)
    }
    movement_headers = {
        str(movement_sheet.cell(1, column).value or "").strip().casefold(): column
        for column in range(1, movement_sheet.max_column + 1)
    }
    required_stock = {"material", "starting stock"}
    required_movements = {"date", "house id", "material", "qty", "movement"}
    if not required_stock.issubset(stock_headers) or not required_movements.issubset(
        movement_headers
    ):
        raise ApiError(400, "Workbook columns do not match the expected tracker layout.")

    warnings = []
    material_ids = {}
    used_names = set()
    for row_number in range(2, stock.max_row + 1):
        name = stock.cell(row_number, stock_headers["material"]).value
        if name is None or not str(name).strip():
            continue
        name = str(name).strip()
        normalized = name.casefold()
        if normalized in used_names:
            raise ApiError(400, f"Duplicate material name in STOCK row {row_number}.")
        used_names.add(normalized)
        raw_qty = stock.cell(row_number, stock_headers["starting stock"]).value
        opening_qty = None
        if raw_qty is not None and str(raw_qty).strip() not in {"", "?"}:
            opening_qty = parse_quantity(raw_qty, f"STOCK row {row_number} starting stock", True)
        else:
            warnings.append(f"{name}: starting stock is unknown; record a stocktake before collection.")
        code_col = stock_headers.get("material id")
        code = stock.cell(row_number, code_col).value if code_col else None
        cursor = db.execute(
            "INSERT INTO materials(code, name, opening_qty) VALUES (?, ?, ?)",
            (str(code).strip() if code is not None else "", name, opening_qty),
        )
        material_ids[normalized] = cursor.lastrowid

    def value(row_number, column):
        index = movement_headers.get(column)
        return movement_sheet.cell(row_number, index).value if index else None

    missing_accountability = 0
    skipped_zero_movements = 0
    for row_number in range(2, movement_sheet.max_row + 1):
        name = value(row_number, "material")
        if name is None or not str(name).strip():
            continue
        name = str(name).strip()
        raw_qty = value(row_number, "qty")
        try:
            qty = parse_quantity(
                raw_qty, f"MOVEMENTS row {row_number} quantity", allow_zero=True
            )
        except ApiError as exc:
            raise ApiError(400, str(exc))
        if qty == 0:
            skipped_zero_movements += 1
            continue

        happened_on = parse_legacy_date(value(row_number, "date"), row_number)

        original_kind = str(value(row_number, "movement") or "").strip().casefold()
        kind_map = {
            "delivery to house": "delivery",
            "return to warehouse": "return",
            "used on house": "use",
        }
        kind = kind_map.get(original_kind)
        if kind is None:
            raise ApiError(400, f"MOVEMENTS row {row_number} has an unsupported movement type.")
        house_id = value(row_number, "house id")
        if house_id is None or not str(house_id).strip():
            raise ApiError(400, f"MOVEMENTS row {row_number} is missing its House ID.")
        house_id = str(house_id).strip()
        normalized = name.casefold()
        material_id = material_ids.get(normalized)
        if material_id is None:
            cursor = db.execute(
                "INSERT INTO materials(code, name, opening_qty) VALUES ('', ?, 0)",
                (name,),
            )
            material_id = cursor.lastrowid
            material_ids[normalized] = material_id
            warnings.append(f"{name}: no STOCK row found; imported with zero opening stock.")
        _upsert_house(
            db,
            house_id,
            value(row_number, "postcode / eircode"),
            value(row_number, "house owner name"),
        )
        source = location_key("house", house_id) if kind in {"return", "use"} else "warehouse"
        destination = "warehouse" if kind == "return" else (
            "consumed" if kind == "use" else location_key("house", house_id)
        )
        person = str(value(row_number, "delivered by") or "").strip()
        team = str(value(row_number, "worker team") or "").strip()
        if not person or not team:
            missing_accountability += 1
        notes = str(value(row_number, "notes") or "").strip()
        db.execute(
            """
            INSERT INTO movements
                (happened_on, kind, material_id, qty, source, destination, house_id, delivery_number, person, team, department, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                happened_on, kind, material_id, qty, source, destination, house_id,
                _next_delivery_number(db, house_id) if kind == "delivery" else None,
                person, team, "", notes,
            ),
        )

    if missing_accountability:
        warnings.append(
            f"{missing_accountability} historical movement(s) have no person or team recorded."
        )
    if skipped_zero_movements:
        warnings.append(
            f"{skipped_zero_movements} zero-quantity movement row(s) were skipped."
        )
    negative_balances = 0
    for row in db.execute("SELECT id FROM materials"):
        for location, quantity in balances_for(db, row["id"]).items():
            if location != "consumed" and quantity is not None and quantity < -1e-9:
                negative_balances += 1
    if negative_balances:
        warnings.append(
            f"{negative_balances} warehouse, team or house balance(s) are negative; reconcile them with physical stocktakes."
        )
    return {
        "material_count": len(material_ids),
        "movement_count": db.execute("SELECT COUNT(*) FROM movements").fetchone()[0],
        "house_count": db.execute("SELECT COUNT(*) FROM houses").fetchone()[0],
        "warnings": warnings,
    }


class LegacyRouteHandlers:
    def _send_json(self, status, payload, headers=()):
        path = urlparse(self.path).path
        if (
            self.command == "POST"
            and status < 400
            and path.startswith("/api/")
        ):
            _record_change(path)
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for name, value in headers:
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path, content_type):
        try:
            body = path.read_bytes()
        except OSError:
            self._send_json(404, {"error": "Page not found."})
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _send_pdf(self, filename, body, attachment=False):
        self.send_response(200)
        self.send_header("Content-Type", "application/pdf")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header(
            "Content-Disposition",
            f"{'attachment' if attachment else 'inline'}; filename*=UTF-8''{quote(filename, safe='')}",
        )
        self.end_headers()
        self.wfile.write(body)

    def _send_png(self, body, headers=()):
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for name, value in headers:
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def _send_house_photo(self, filename, content, content_type):
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header(
            "Content-Disposition",
            f"inline; filename*=UTF-8''{quote(filename, safe='')}",
        )
        self.end_headers()
        self.wfile.write(content)

    def _body(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise ApiError(400, "Invalid request body.")
        if length <= 0 or length > 10_000_000:
            raise ApiError(400, "Request body is empty or too large.")
        try:
            return self.rfile.read(length)
        except OSError:
            raise ApiError(400, "Could not read request body.")

    def _json_body(self):
        try:
            return json.loads(self._body())
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise ApiError(400, "Request body must be valid JSON.")

    def _session_cookie(self):
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
        except CookieError:
            return ""
        morsel = cookie.get("ewi_session")
        return morsel.value if morsel else ""

    def _current_user(self):
        token = self._session_cookie()
        if not token:
            return None
        token_hash = hashlib.sha256(token.encode("ascii", errors="ignore")).hexdigest()
        now = int(datetime.now().timestamp())
        with connect_db() as db:
            return db.execute(
                """
                SELECT u.id, u.username, u.display_name, u.role,
                       u.supervisor_specialty, u.team_name,
                       roles.display_name AS role_name, s.token_hash,
                       COALESCE((
                           SELECT group_concat(permission_key, char(31))
                           FROM app_role_permissions
                           WHERE role_key = u.role AND is_allowed = 1
                       ), '') AS role_permissions,
                       COALESCE((
                           SELECT json_group_array(json_object(
                               'id', departments.id,
                               'name', departments.name,
                               'is_primary', memberships.is_primary
                           ))
                           FROM user_departments memberships
                           JOIN departments ON departments.id = memberships.department_id
                           WHERE memberships.user_id = u.id
                       ), '[]') AS departments_json
                FROM app_sessions s
                JOIN app_users u ON u.id = s.user_id
                LEFT JOIN app_roles roles ON roles.role_key = u.role
                WHERE s.token_hash = ? AND s.expires_at > ? AND u.is_active = 1
                """,
                (token_hash, now),
            ).fetchone()

    def _login_required(self):
        user = self._current_user()
        if user is None:
            self._send_json(401, {"error": "Sign in to continue."})
            return None
        path = urlparse(self.path).path
        is_plumbing_coordinator = _has_access_profile(user, "plumbing_coordinator")
        plumbing_photo_read_allowed = False
        ventilation_coordinator_read_allowed = False
        if (
            self.command == "GET"
            and _has_access_profile(user, "ventilation_coordinator")
        ):
            with connect_db() as db:
                if path.startswith("/api/houses/"):
                    house_id = unquote(path[len("/api/houses/"):])
                    ventilation_coordinator_read_allowed = (
                        bool(house_id)
                        and "/" not in house_id
                        and _ventilation_coordinator_house_is_visible(db, house_id)
                    )
                elif path.startswith("/api/house-vent-photos/"):
                    photo_path = path[
                        len("/api/house-vent-photos/"):
                    ].split("/")
                    if (
                        len(photo_path) == 2
                        and photo_path[0].isdigit()
                        and photo_path[1] == "file"
                    ):
                        photo = db.execute(
                            """
                            SELECT house_id FROM house_vent_photos
                            WHERE id = ?
                            """,
                            (int(photo_path[0]),),
                        ).fetchone()
                        ventilation_coordinator_read_allowed = bool(
                            photo
                            and _ventilation_coordinator_house_is_visible(
                                db, photo["house_id"]
                            )
                        )
        if self.command == "GET" and (
            path.startswith("/api/houses/")
            or path.startswith("/api/house-plumbing-photos/")
        ):
            with connect_db() as db:
                if is_plumbing_coordinator:
                    if path.startswith("/api/houses/"):
                        house_id = unquote(path[len("/api/houses/"):])
                        plumbing_photo_read_allowed = (
                            bool(house_id)
                            and "/" not in house_id
                            and _plumbing_coordinator_house_is_visible(db, house_id)
                        )
                    else:
                        photo_path = path[
                            len("/api/house-plumbing-photos/"):
                        ].split("/")
                        if (
                            len(photo_path) == 2
                            and photo_path[0].isdigit()
                            and photo_path[1] == "file"
                        ):
                            photo = db.execute(
                                """
                                SELECT house_id FROM house_plumbing_photos
                                WHERE id = ?
                                """,
                                (int(photo_path[0]),),
                            ).fetchone()
                            plumbing_photo_read_allowed = bool(
                                photo
                                and _plumbing_coordinator_house_is_visible(
                                    db, photo["house_id"]
                                )
                            )
                else:
                    plumbing_photo_read_allowed = _can_view_plumbing_house_photos(
                        db, user
                    )
        if is_plumbing_coordinator and not (
            (
                self.command == "GET"
                and (
                    path in {"/api/dashboard", "/api/updates"}
                    or plumbing_photo_read_allowed
                )
            )
            or (
                self.command == "POST"
                and path in {
                    "/api/auth/pin",
                    "/api/auth/logout",
                    "/api/plumbing-coordinator/assignment",
                    "/api/house-work-plan/complete",
                }
            )
        ):
            self._send_json(
                403,
                {"error": "Plumbing Coordinator access is limited to plumbing assignments."},
            )
            return None
        if _has_access_profile(user, "ventilation_coordinator") and not (
            (
                self.command == "GET"
                and (
                    path in {"/api/dashboard", "/api/updates"}
                    or ventilation_coordinator_read_allowed
                )
            )
            or (
                self.command == "POST"
                and path in {
                    "/api/auth/pin",
                    "/api/auth/logout",
                    "/api/ventilation-coordinator/assignment",
                    "/api/ventilation-coordinator/insulation-assignment",
                    "/api/house-work-plan/complete",
                }
            )
        ):
            self._send_json(
                403,
                {"error": "Vent Coordinator access is limited to ventilation assignments."},
            )
            return None
        if not any(
            _has_access_profile(user, profile)
            for profile in (
                "admin",
                "assign",
                "boss",
                "surveyor",
                "warehouse",
                "delivery",
                "supervisor",
                "teamlead",
                "plumber",
                "plumbing_coordinator",
                "ventilation_coordinator",
                "employee",
            )
        ) and not plumbing_photo_read_allowed and path not in {
            "/api/auth/pin",
            "/api/auth/logout",
        }:
            self._send_json(
                403,
                {"error": "This account has no active access permissions. Contact an administrator."},
            )
            return None
        if _has_access_profile(user, "assign") and path in {
            "/api/calculator/requests",
            "/api/import",
            "/api/materials",
            "/api/movements",
            "/api/movements/batch",
            "/api/stocktakes",
            "/api/houses/status",
        }:
            self._send_json(
                403,
                {"error": "Assign accounts cannot access material information."},
            )
            return None
        if _has_access_profile(user, "surveyor"):
            allowed = self.command == "GET" and self.path.split("?", 1)[0] == "/api/calculator/houses"
            allowed = allowed or (
                self.command == "GET"
                and (
                    urlparse(self.path).path == "/api/updates"
                    or (
                        urlparse(self.path).path.startswith("/api/houses/")
                            and (
                                _has_access_profile(user, "vent_photos")
                                or plumbing_photo_read_allowed
                            )
                    )
                    or urlparse(self.path).path == "/api/house-documents"
                    or urlparse(self.path).path.startswith("/api/house-documents/")
                    or urlparse(self.path).path == "/api/seai-documents"
                    or urlparse(self.path).path.startswith("/api/seai-documents/")
                    or re.fullmatch(
                        r"/api/survey-room-test-pdfs/[a-z0-9-]+\.pdf(?:/preview)?",
                        urlparse(self.path).path,
                    )
                    or urlparse(self.path).path == "/api/house-vent-documents"
                    or urlparse(self.path).path.startswith("/api/house-vent-documents/")
                    or urlparse(self.path).path.startswith("/api/house-vent-photos/")
                    or urlparse(self.path).path.startswith("/api/house-plumbing-photos/")
                    or urlparse(self.path).path == "/api/supervisor/shared-seai-documents"
                    or urlparse(self.path).path.startswith("/api/supervisor/shared-seai-documents/")
                )
            )
            allowed = allowed or (
                self.command == "POST"
                and urlparse(self.path).path == "/api/house-vent-documents"
            )
            allowed = allowed or (
                self.command == "POST"
                and re.fullmatch(
                    r"/api/house-vent-photos/\d+/delete",
                    urlparse(self.path).path,
                )
                is not None
            )
            allowed = allowed or (
                self.command == "POST"
                and re.fullmatch(
                    r"/api/house-plumbing-photos/\d+/delete",
                    urlparse(self.path).path,
                )
                is not None
            )
            allowed = allowed or (
                self.command == "POST"
                and urlparse(self.path).path == "/api/supervisor/shared-seai-documents"
            )
            allowed = allowed or (
                self.command == "POST"
                and urlparse(self.path).path
                in {
                    "/api/house-survey",
                    "/api/house-notes",
                    "/api/survey-notes",
                    "/api/house-work-plan",
                    "/api/house-work-plan/complete",
                    "/api/seai-measure-variation",
                    "/api/seai-documents/share",
                    "/api/auth/pin",
                }
            )
            allowed = allowed or (
                self.command == "POST"
                and re.fullmatch(
                    r"/api/seai-documents/(?:save-selected|\d+/delete-selected)",
                    urlparse(self.path).path,
                )
                is not None
            )
            allowed = allowed or (
                self.command == "POST"
                and urlparse(self.path).path == "/api/house-documents"
            )
            allowed = allowed or (
                self.command == "POST"
                and re.fullmatch(
                    r"/api/house-documents/\d+/annotations",
                    urlparse(self.path).path,
                )
                is not None
            )
            allowed = allowed or (
                self.command == "POST"
                and re.fullmatch(
                    r"/api/house-documents/\d+/delete",
                    urlparse(self.path).path,
                )
                is not None
            )
            allowed = allowed or (
                self.command == "POST"
                and re.fullmatch(
                    r"/api/seai-documents/\d+/annotations",
                    urlparse(self.path).path,
                )
                is not None
            )
            allowed = allowed or (
                self.command == "POST"
                and re.fullmatch(
                    r"/api/survey-room-test-pdfs/[a-z0-9-]+\.pdf/annotations",
                    urlparse(self.path).path,
                )
                is not None
            )
            allowed = allowed or (
                self.command == "POST"
                and re.fullmatch(
                    r"/api/seai-documents/\d+/share",
                    urlparse(self.path).path,
                )
                is not None
            )
            allowed = allowed or (
                self.command == "POST"
                and re.fullmatch(
                    r"/api/seai-documents/\d+/share-with-ewi-team",
                    urlparse(self.path).path,
                )
                is not None
            )
            if not allowed:
                self._send_json(403, {"error": "Surveyor access is limited to ready-house surveys."})
                return None
        return user

    def _admin_required(self, user):
        if not _has_access_profile(user, "admin"):
            self._send_json(403, {"error": "Administrator access is required."})
            return False
        return True

    def _session_headers(self, token, max_age=SESSION_MAX_AGE):
        attributes = [
            f"ewi_session={token}",
            "Path=/",
            "HttpOnly",
            "SameSite=Lax",
            f"Max-Age={max_age}",
        ]
        if max_age > 0:
            expires = datetime.now(timezone.utc) + timedelta(seconds=max_age)
            attributes.append(f"Expires={expires.strftime('%a, %d %b %Y %H:%M:%S GMT')}")
        forwarded_proto = str(self.headers.get("x-forwarded-proto", "")).strip().lower()
        if COOKIE_SECURE or (
            TRUST_CLOUDFLARE_PROXY and forwarded_proto == "https"
        ):
            attributes.append("Secure")
        return [("Set-Cookie", "; ".join(attributes))]

    def _expired_session_headers(self):
        return self._session_headers("", 0)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/":
            self._send_file(STATIC_DIR / "index.html", "text/html; charset=utf-8")
        elif path == "/oldest-house-test":
            user = self._login_required()
            if user is None:
                return
            if not (_has_access_profile(user, "admin") or _has_access_profile(user, "surveyor")):
                self._send_json(
                    403,
                    {"error": "Surveyor access is required to view house photos."},
                )
                return
            self._send_file(
                STATIC_DIR / "oldest-house-test.html", "text/html; charset=utf-8"
            )
        elif path.startswith("/oldest-house-test/photos/"):
            user = self._login_required()
            if user is None:
                return
            if not (_has_access_profile(user, "admin") or _has_access_profile(user, "surveyor")):
                self._send_json(
                    403,
                    {"error": "Surveyor access is required to view house photos."},
                )
                return
            photo_name = path.rsplit("/", 1)[-1]
            photos = {
                "front.svg": STATIC_DIR / "oldest-house-test" / "synthetic" / "photos" / "front.svg",
                "rear.svg": STATIC_DIR / "oldest-house-test" / "synthetic" / "photos" / "rear.svg",
                "gable.svg": STATIC_DIR / "oldest-house-test" / "synthetic" / "photos" / "gable.svg",
            }
            photo = photos.get(photo_name)
            if photo is None:
                self._send_json(404, {"error": "Photo not found."})
                return
            self._send_file(photo, "image/svg+xml")
        elif path.startswith("/oldest-house-test/pdfs/"):
            user = self._login_required()
            if user is None:
                return
            if not (_has_access_profile(user, "admin") or _has_access_profile(user, "surveyor")):
                self._send_json(
                    403,
                    {"error": "Surveyor access is required to view house documents."},
                )
                return
            pdf_name = path.rsplit("/", 1)[-1]
            pdfs = {
                "application-letter.pdf": (
                    "Synthetic Demo Application.pdf",
                    STATIC_DIR / "oldest-house-test" / "synthetic" / "pdfs" / "application-letter.pdf",
                ),
                "survey-report.pdf": (
                    "Synthetic Survey Report.pdf",
                    STATIC_DIR / "oldest-house-test" / "synthetic" / "pdfs" / "survey-report.pdf",
                ),
                "survey-sketch.pdf": (
                    "Synthetic Survey Sketch.pdf",
                    STATIC_DIR / "oldest-house-test" / "synthetic" / "pdfs" / "survey-sketch.pdf",
                ),
                "floor-plan-1.pdf": (
                    "Synthetic Floor Plan - Ground.pdf",
                    STATIC_DIR / "oldest-house-test" / "synthetic" / "pdfs" / "floor-plan-1.pdf",
                ),
                "floor-plan-2.pdf": (
                    "Synthetic Floor Plan - First.pdf",
                    STATIC_DIR / "oldest-house-test" / "synthetic" / "pdfs" / "floor-plan-2.pdf",
                ),
                "floor-plan-3.pdf": (
                    "Synthetic Floor Plan - Alternate.pdf",
                    STATIC_DIR / "oldest-house-test" / "synthetic" / "pdfs" / "floor-plan-3.pdf",
                ),
            }
            pdf = pdfs.get(pdf_name)
            if pdf is None:
                self._send_json(404, {"error": "PDF not found."})
                return
            try:
                content = pdf[1].read_bytes()
            except OSError:
                self._send_json(404, {"error": "PDF not found."})
                return
            self._send_pdf(pdf[0], content)
        elif path == "/app.css":
            self._send_file(STATIC_DIR / "app.css", "text/css; charset=utf-8")
        elif path == "/app.js":
            self._send_file(STATIC_DIR / "app.js", "text/javascript; charset=utf-8")
        elif path.startswith("/ventilation-icons/"):
            icon_directory = (STATIC_DIR / "ventilation-icons").resolve()
            icon_name = unquote(path[len("/ventilation-icons/"):])
            icon_name = {
                "core-drill-ventilator.svg": "5” Core Drill Ventilator.svg",
            }.get(icon_name, icon_name)
            icon_path = (icon_directory / icon_name).resolve()
            if (
                icon_path.parent != icon_directory
                or icon_path.suffix.lower() != ".svg"
                or not icon_path.is_file()
            ):
                self._send_json(404, {"error": "Ventilation icon not found."})
                return
            self._send_file(icon_path, "image/svg+xml")
        elif path == "/brusna-curious-cat-logo.png":
            self._send_file(STATIC_DIR / "brusna-curious-cat-logo.png", "image/png")
        elif path == "/favicon.svg":
            self._send_file(STATIC_DIR / "favicon.svg", "image/svg+xml")
        elif path == "/brusna-cat-favicon-v2.svg":
            self._send_file(STATIC_DIR / "favicon.svg", "image/svg+xml")
        elif path == "/favicon.ico" or path == "/brusna-cat-v3.ico":
            self._send_file(STATIC_DIR / "brusna-cat-v3.ico", "image/x-icon")
        elif path == "/brusna-cat-v3.png":
            self._send_file(STATIC_DIR / "brusna-cat-v3.png", "image/png")
        elif path == "/calculator":
            user = self._current_user()
            if user is not None and _has_access_profile(user, "assign"):
                self._send_json(
                    403,
                    {"error": "Assign accounts cannot access material information."},
                )
                return
            self._send_file(ROOT / "Auto_calc.html", "text/html; charset=utf-8")
        elif path == "/api/calculator/houses":
            user = self._login_required()
            if user is None:
                return
            params = parse_qs(urlparse(self.path).query)
            query = params.get("q", [""])[0].strip()
            status_filter = params.get("status", [""])[0]
            if _has_access_profile(user, "surveyor"):
                status_filter = "ready"
            wanted = {
                "not_started": "h.id IS NULL AND p.ready_for_start = 0",
                "ready": "p.ready_for_start = 1",
                "pending": "h.id IS NULL",
                "all": "1 = 1",
                "map": "((h.id IS NOT NULL AND h.is_finished = 0 "
                       "AND p.ready_for_start = 0) OR "
                       "(h.id IS NULL AND (p.ready_for_start = 1 OR "
                       "(p.latitude IS NOT NULL AND p.longitude IS NOT NULL))) "
                       "OR (p.ready_for_start = 1))",
            }.get(status_filter)
            if status_filter == "ready":
                ordering = """
                    (SELECT changed_at FROM house_status_history
                     WHERE house_id = p.id AND status = 'ready'
                     ORDER BY id DESC LIMIT 1) DESC,
                    p.id COLLATE NOCASE
                """
            else:
                ordering = "(h.is_finished = 1), p.id COLLATE NOCASE"
            surveyor_assignment_filter = ""
            if _has_access_profile(user, "surveyor"):
                surveyor_assignment_filter = """
                    AND EXISTS (
                        SELECT 1 FROM house_role_assignments surveyor_scope
                        WHERE surveyor_scope.house_id = p.id
                          AND surveyor_scope.role = 'surveyor'
                          AND surveyor_scope.user_id = ?
                    )
                """
            like = "%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            with connect_db() as db:
                rows = db.execute(
                    f"""
                    SELECT p.id, p.postcode, p.address, p.owner_name,
                           COALESCE(h.employee_name, '') AS employee_name,
                           p.area_m2, p.perimeter_m, p.latitude, p.longitude,
                           COALESCE(h.stage, 1) AS stage, p.notes, p.source,
                           p.plan_scaffolding, p.plan_esb, p.plan_plumber,
                           COALESCE(hc.plumber_status, p.plan_plumber) AS plumber_status,
                           p.plan_scaffolding_required, p.plan_scaffolding_notes,
                           p.plan_esb_required, p.plan_esb_notes,
                           p.plan_plumber_required, p.plan_plumber_notes,
                           p.plan_scaffolding_done_by, p.plan_scaffolding_done_at,
                           p.plan_esb_done_by, p.plan_esb_done_at,
                           p.plan_plumber_done_by, p.plan_plumber_done_at,
                           p.plan_vent_required, p.plan_vent_notes,
                           p.plan_vent_done_by, p.plan_vent_done_at,
                           p.plan_ceramic_fuse_board, p.plan_solar_pv_required,
                           p.plan_pir_required, p.plan_order_variations,
                           p.plan_windows_required,
                           p.plan_ceramic_fuse_board_done_by,
                           p.plan_ceramic_fuse_board_done_at,
                           p.plan_solar_pv_required_done_by,
                           p.plan_solar_pv_required_done_at,
                           p.plan_pir_required_done_by,
                           p.plan_pir_required_done_at,
                           p.plan_order_variations_done_by,
                           p.plan_order_variations_done_at,
                           p.plan_windows_required_done_by,
                           p.plan_windows_required_done_at,
                           p.plan_scaffolding_done_by_user_id,
                           p.plan_esb_done_by_user_id,
                           p.plan_plumber_done_by_user_id,
                           p.plan_vent_done_by_user_id,
                           p.plan_ceramic_fuse_board_done_by_user_id,
                           p.plan_solar_pv_required_done_by_user_id,
                           p.plan_pir_required_done_by_user_id,
                           p.plan_order_variations_done_by_user_id,
                           p.plan_windows_required_done_by_user_id,
                           hs.data AS survey_data, hs.notes AS survey_notes,
                           hs.updated_by AS survey_updated_by, hs.updated_at AS survey_updated_at,
                           assigned_surveyor.display_name AS assigned_surveyor_name,
                           assigned_surveyor.is_active AS assigned_surveyor_is_active,
                           surveyor_assignment.assigned_by AS surveyor_assigned_by,
                           surveyor_assignment.assigned_at AS surveyor_assigned_at,
                           EXISTS (
                               SELECT 1 FROM house_role_assignments assignment
                               JOIN app_users assigned_user ON assigned_user.id = assignment.user_id
                               WHERE assignment.house_id = p.id
                                 AND assignment.role = 'surveyor'
                                 AND assigned_user.is_active = 1
                                 AND EXISTS (
                                     SELECT 1 FROM app_role_permissions access
                                     WHERE access.role_key = assigned_user.role
                                       AND access.permission_key = 'access.surveyor'
                                       AND access.is_allowed = 1
                                 )
                           ) AS has_assigned_surveyor,
                           EXISTS (
                               SELECT 1 FROM house_role_assignments assignment
                               JOIN app_users assigned_user ON assigned_user.id = assignment.user_id
                               WHERE assignment.house_id = p.id
                                 AND assignment.role = 'supervisor'
                                 AND assigned_user.is_active = 1
                                 AND EXISTS (
                                     SELECT 1 FROM app_role_permissions access
                                     WHERE access.role_key = assigned_user.role
                                       AND access.permission_key = 'access.supervisor'
                                       AND access.is_allowed = 1
                                 )
                           ) AS has_assigned_supervisor,
                           CASE WHEN p.ready_for_start = 1 THEN 'ready'
                                WHEN h.id IS NULL THEN 'not_started'
                                WHEN h.is_finished = 1 THEN 'closed'
                                ELSE 'active' END AS status,
                           (h.id IS NOT NULL AND p.ready_for_start = 1) AS is_returned_house
                    FROM provided_houses p
                    LEFT JOIN houses h ON h.id = p.id
                    LEFT JOIN house_checklists hc ON hc.house_id = p.id
                    LEFT JOIN house_surveys hs ON hs.house_id = p.id
                    LEFT JOIN house_role_assignments surveyor_assignment
                      ON surveyor_assignment.house_id = p.id
                     AND surveyor_assignment.role = 'surveyor'
                    LEFT JOIN app_users assigned_surveyor
                      ON assigned_surveyor.id = surveyor_assignment.user_id
                    WHERE (p.id LIKE ? ESCAPE '\\' OR p.postcode LIKE ? ESCAPE '\\'
                       OR p.owner_name LIKE ? ESCAPE '\\' OR p.address LIKE ? ESCAPE '\\')
                      AND ({wanted or "1 = 1"})
                      {surveyor_assignment_filter}
                    ORDER BY {ordering}
                    LIMIT ?
                    """,
                    (
                        like,
                        like,
                        like,
                        like,
                        *([user["id"]] if _has_access_profile(user, "surveyor") else []),
                        500 if status_filter in {"all", "map"} or wanted else 20,
                    ),
                ).fetchall()
                surveyor_work_plan_drafts = {}
                if (
                    _has_access_profile(user, "admin")
                    or _has_access_profile(user, "surveyor")
                ) and rows:
                    house_placeholders = ",".join("?" for _ in rows)
                    for draft in db.execute(
                        "SELECT house_id, data, saved_by, saved_at "
                        "FROM house_work_plan_drafts "
                        f"WHERE house_id IN ({house_placeholders})",
                        [row["id"] for row in rows],
                    ):
                        try:
                            surveyor_work_plan_drafts[draft["house_id"]] = {
                                **json.loads(draft["data"]),
                                "saved_by": draft["saved_by"],
                                "saved_at": draft["saved_at"],
                            }
                        except (TypeError, ValueError):
                            raise ApiError(
                                500,
                                f"Saved work-plan draft for {draft['house_id']} is invalid.",
                            )
                seai_details = {}
                seai_summaries = {}
                can_see_seai_summary = (
                    _has_access_profile(user, "admin")
                    or _has_access_profile(user, "surveyor")
                    or _has_access_profile(user, "assign")
                    or _has_access_profile(user, "supervisor")
                )
                if rows and can_see_seai_summary:
                    has_seai_table = db.execute(
                        "SELECT 1 FROM sqlite_master WHERE type = 'table' "
                        "AND name = 'seai_applications'"
                    ).fetchone()
                    if has_seai_table:
                        id_placeholders = ",".join("?" for _ in rows)
                        for detail in db.execute(
                            "SELECT application_id, detail_data_json FROM seai_applications "
                            f"WHERE application_id IN ({id_placeholders})",
                            [row["id"] for row in rows],
                        ):
                            try:
                                summary = _seai_card_summary(json.loads(detail["detail_data_json"]))
                            except (TypeError, ValueError):
                                summary = None
                            if summary:
                                seai_summaries[detail["application_id"]] = summary
                if (_has_access_profile(user, "admin") or _has_access_profile(user, "surveyor")) and rows:
                    has_seai_applications = db.execute(
                        "SELECT 1 FROM sqlite_master WHERE type = 'table' "
                        "AND name = 'seai_applications'"
                    ).fetchone()
                    if has_seai_applications:
                        placeholders = ",".join("?" for _ in rows)
                        seai_details = {
                            detail["application_id"]: json.loads(
                                detail["detail_data_json"]
                            )
                            for detail in db.execute(
                                "SELECT application_id, detail_data_json "
                                "FROM seai_applications "
                                f"WHERE application_id IN ({placeholders})",
                                [row["id"] for row in rows],
                            )
                        }
                snags = _snags_by_house(db)
                house_audit = _house_audit_by_id(db)
                work_plan_permissions = {
                    trade: _can_complete_work_plan_trade(db, user, trade)
                    for trade in WORK_PLAN_TRADES
                }
                work_plan_permissions.update(
                    {
                        question: (
                            _has_access_profile(user, "assign")
                            or _has_access_profile(user, "surveyor")
                        )
                        for question in WORK_PLAN_SURVEY_QUESTIONS
                    }
                )
                seai_application_ids = _seai_application_ids_by_house(
                    db, [row["id"] for row in rows]
                )
            houses = []
            for row in rows:
                house = dict(row)
                house["seai_application_id"] = seai_application_ids.get(
                    row["id"], ""
                )
                if (
                    _has_access_profile(user, "admin")
                    or _has_access_profile(user, "surveyor")
                ):
                    draft = surveyor_work_plan_drafts.get(row["id"], {})
                    for trade in WORK_PLAN_TRADES:
                        house[f"draft_plan_{trade}_required"] = draft.get(
                            f"{trade}_required"
                        )
                        house[f"draft_plan_{trade}_notes"] = draft.get(
                            f"{trade}_notes", ""
                        )
                    for question in WORK_PLAN_SURVEY_QUESTIONS:
                        house[f"draft_plan_{question}"] = draft.get(question)
                    house["work_plan_draft_saved_at"] = draft.get("saved_at")
                if not _has_access_profile(user, "surveyor"):
                    house.pop("surveyor_assigned_by", None)
                house["work_plan_permissions"] = work_plan_permissions
                if can_see_seai_summary:
                    house["seai_summary"] = seai_summaries.get(row["id"])
                if (_has_access_profile(user, "admin") or _has_access_profile(user, "surveyor")):
                    house["seai_details"] = seai_details.get(row["id"])
                raw_survey = house.pop("survey_data", None)
                survey_data = json.loads(raw_survey) if raw_survey else {}
                house["survey_completed"] = _survey_has_valid_perimeter(survey_data)
                if _has_access_profile(user, "assign"):
                    house["survey"] = bool(raw_survey)
                    house["has_surveyed_perimeter"] = house["survey_completed"]
                else:
                    house["survey"] = survey_data or None
                house["snags"] = snags.get(row["id"], [])
                house.update(
                    house_audit.get(
                        row["id"], {"status_history": [], "last_movement": None}
                    )
                )
                if _has_access_profile(user, "assign"):
                    house.pop("last_movement", None)
                    house["activity_history"] = [
                        entry
                        for entry in house.get("activity_history", [])
                        if entry.get("type") not in {"movement", "material_request"}
                    ]
                houses.append(house)
            self._send_json(200, {"houses": houses})
        elif path == "/api/house-assignments":
            user = self._login_required()
            if user is None:
                return
            if not (_has_access_profile(user, "admin") or _has_access_profile(user, "assign")):
                self._send_json(
                    403,
                    {"error": "Administrator or Assign access is required to manage house assignments."},
                )
                return
            house_id = parse_qs(urlparse(self.path).query).get("house_id", [""])[0].strip()
            if not house_id:
                self._send_json(400, {"error": "A house ID is required."})
                return
            with connect_db() as db:
                house = db.execute(
                    """
                    SELECT p.id, p.address, p.postcode, p.owner_name,
                           hs.data AS survey_data, h.id IS NOT NULL AS is_returned_house
                    FROM provided_houses p
                    LEFT JOIN houses h ON h.id = p.id
                    LEFT JOIN house_surveys hs ON hs.house_id = p.id
                    WHERE p.id = ?
                      AND (h.id IS NULL OR h.is_finished = 0)
                    """,
                    (house_id,),
                ).fetchone()
                if not house:
                    self._send_json(404, {"error": "Available house was not found."})
                    return
                users = {
                    role: [
                        dict(row)
                        for row in db.execute(
                            """
                            SELECT id, username, display_name,
                                   (
                                       supervisor_specialty = 'External Wall Insulation' COLLATE NOCASE
                                       OR EXISTS (
                                           SELECT 1 FROM user_departments link
                                           JOIN departments dept ON dept.id = link.department_id
                                           WHERE link.user_id = app_users.id
                                             AND dept.name = 'External Wall Insulation' COLLATE NOCASE
                                       )
                                   ) AS is_ewi
                            FROM app_users
                            WHERE is_active = 1
                              AND EXISTS (
                                  SELECT 1 FROM app_role_permissions access
                                  WHERE access.role_key = app_users.role
                                    AND access.permission_key = ?
                                    AND access.is_allowed = 1
                              )
                            ORDER BY display_name COLLATE NOCASE, username COLLATE NOCASE
                            """,
                            (f"access.{role}",),
                        )
                    ]
                    for role in ("surveyor", "supervisor")
                }
                assignments = [
                    dict(row)
                    for row in db.execute(
                        """
                        SELECT assignment.role, user.id AS user_id, user.username,
                               user.display_name, user.is_active, user.role AS account_role
                        FROM house_role_assignments assignment
                        JOIN app_users user ON user.id = assignment.user_id
                        WHERE assignment.house_id = ?
                        """,
                        (house_id,),
                    )
                ]
                raw_survey = house["survey_data"]
                try:
                    survey_data = json.loads(raw_survey) if raw_survey else {}
                except (TypeError, json.JSONDecodeError):
                    self._send_json(500, {"error": "Stored house survey data is invalid."})
                    return
            try:
                perimeter = float(survey_data.get("perimeter_m"))
            except (AttributeError, TypeError, ValueError, OverflowError):
                perimeter = 0
            self._send_json(
                200,
                {
                    "house": {
                        key: house[key]
                        for key in (
                            "id",
                            "address",
                            "postcode",
                            "owner_name",
                            "is_returned_house",
                        )
                    },
                    "users": users,
                    "assignments": assignments,
                    "requirements": {
                        "has_survey": bool(raw_survey),
                        "has_surveyed_perimeter": math.isfinite(perimeter)
                        and 0 < perimeter <= 10000,
                        "has_assigned_surveyor": any(
                            assignment["role"] == "surveyor"
                            and assignment["is_active"]
                            and assignment["account_role"] == "surveyor"
                            for assignment in assignments
                        ),
                    },
                },
            )
        elif path == "/api/house-documents":
            user = self._login_required()
            if user is None:
                return
            is_assign = _has_access_profile(user, "assign")
            with connect_db() as db:
                is_plumbing_supervisor = _is_plumbing_supervisor(db, user)
                is_ewi_supervisor = _is_ewi_supervisor(db, user)
            has_house_document_access = (
                _has_access_profile(user, "admin")
                or _has_access_profile(user, "surveyor")
                or is_assign
                or is_plumbing_supervisor
                or is_ewi_supervisor
            )
            if not has_house_document_access:
                self._send_json(403, {"error": "Administrator, Surveyor, Assign, or assigned Supervisor access is required to view house documents."})
                return
            house_id = parse_qs(urlparse(self.path).query).get("house_id", [""])[0].strip()
            if not house_id:
                self._send_json(400, {"error": "A house ID is required."})
                return
            with connect_db() as db:
                available = db.execute(
                    """
                    SELECT 1 FROM provided_houses p LEFT JOIN houses h ON h.id = p.id
                    WHERE p.id = ?
                      AND (? = 1 OR h.id IS NULL OR p.ready_for_start = 1)
                      AND (
                          ? = 0 OR EXISTS (
                              SELECT 1 FROM house_plumbing_assignments assignment
                              WHERE assignment.house_id = p.id
                                AND assignment.supervisor_user_id = ?
                          ) OR (
                              ? = 1 AND EXISTS (
                                  SELECT 1 FROM house_role_assignments assignment
                                  WHERE assignment.house_id = p.id
                                    AND assignment.role = 'supervisor'
                                    AND assignment.user_id = ?
                              )
                          )
                      )
                    """,
                    (
                        house_id,
                        int(
                            is_assign
                            or is_plumbing_supervisor
                            or is_ewi_supervisor
                        ),
                        int(is_plumbing_supervisor or is_ewi_supervisor),
                        user["id"],
                        int(is_plumbing_supervisor or is_ewi_supervisor),
                        user["id"],
                    ),
                ).fetchone()
                if not available:
                    self._send_json(404, {"error": "House was not found."})
                    return
                rows = db.execute(
                    """
                    SELECT id, filename, size_bytes, uploaded_by, uploaded_at
                    FROM house_documents WHERE house_id = ? ORDER BY uploaded_at DESC, id DESC
                    """,
                    (house_id,),
                ).fetchall()
                not_started = _is_not_started_house(db, house_id)
                if not_started:
                    rows = [
                        row for row in rows
                        if not _is_annotated_pdf_filename(row["filename"])
                    ]
            self._send_json(
                200,
                {
                    "house_id": house_id,
                    "documents": [
                        {
                            **dict(row),
                            "is_annotated": _is_annotated_pdf_filename(
                                row["filename"]
                            ),
                        }
                        for row in rows
                    ],
                },
            )
        elif path == "/api/supervisor/shared-seai-documents" or path.startswith(
            "/api/supervisor/shared-seai-documents/"
        ):
            user = self._login_required()
            if user is None:
                return
            if not _has_access_profile(user, "supervisor"):
                self._send_json(403, {"error": "Supervisor access is required to view shared team PDFs."})
                return
            if path == "/api/supervisor/shared-seai-documents":
                with connect_db() as db:
                    document_table = db.execute(
                        "SELECT 1 FROM sqlite_master WHERE type = 'table' "
                        "AND name = 'seai_documents'"
                    ).fetchone()
                    if not document_table:
                        rows = []
                    else:
                        columns = {
                            row["name"]
                            for row in db.execute("PRAGMA table_info(seai_documents)")
                        }
                        if "content" not in columns:
                            self._send_json(
                                503,
                                {"error": "SEAI PDF storage has not been initialized."},
                            )
                            return
                        rows = [
                            dict(row)
                            for row in db.execute(
                            """
                            SELECT shares.id, shares.house_id, shares.shared_by,
                                   shares.shared_at, documents.filename,
                                   length(documents.content) AS size_bytes,
                                   '/api/supervisor/shared-seai-documents/' ||
                                       shares.id || '/file' AS file_url
                            FROM seai_document_shares shares
                            JOIN seai_documents documents ON documents.id = shares.document_id
                            WHERE shares.recipient_user_id = ?
                              AND documents.content IS NOT NULL
                            ORDER BY shares.shared_at DESC, shares.id DESC
                            """,
                            (user["id"],),
                            ).fetchall()
                        ]
                    rows.extend(
                        dict(row)
                        for row in db.execute(
                            """
                            SELECT 'delivery-' || id AS id, house_id, shared_by,
                                   shared_at, filename, size_bytes,
                                   '/api/supervisor/shared-seai-documents/delivery-' ||
                                       id || '/file' AS file_url
                            FROM seai_shared_pdf_deliveries
                            WHERE recipient_user_id = ?
                            ORDER BY shared_at DESC, id DESC
                            """,
                            (user["id"],),
                        ).fetchall()
                    )
                rows.sort(
                    key=lambda row: (row["shared_at"], str(row["id"])),
                    reverse=True,
                )
                self._send_json(200, {"documents": [dict(row) for row in rows]})
                return
            delivery_match = re.fullmatch(
                r"/api/supervisor/shared-seai-documents/delivery-(\d+)/file",
                path,
            )
            if delivery_match:
                with connect_db() as db:
                    row = db.execute(
                        """
                        SELECT filename, content
                        FROM seai_shared_pdf_deliveries
                        WHERE id = ? AND recipient_user_id = ?
                        """,
                        (int(delivery_match.group(1)), user["id"]),
                    ).fetchone()
                if row is None:
                    self._send_json(404, {"error": "Shared PDF was not found."})
                    return
                attachment = parse_qs(urlparse(self.path).query).get("download", ["0"])[0] == "1"
                self._send_pdf(row["filename"], row["content"], attachment=attachment)
                return
            match = re.fullmatch(
                r"/api/supervisor/shared-seai-documents/(\d+)/file", path
            )
            if match is None:
                self._send_json(404, {"error": "Shared PDF was not found."})
                return
            with connect_db() as db:
                document_table = db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' "
                    "AND name = 'seai_documents'"
                ).fetchone()
                if not document_table:
                    self._send_json(404, {"error": "Shared PDF was not found."})
                    return
                row = db.execute(
                    """
                    SELECT documents.filename, documents.content
                    FROM seai_document_shares shares
                    JOIN seai_documents documents ON documents.id = shares.document_id
                    WHERE shares.id = ? AND shares.recipient_user_id = ?
                      AND documents.content IS NOT NULL
                    """,
                    (int(match.group(1)), user["id"]),
                ).fetchone()
            if row is None:
                self._send_json(404, {"error": "Shared PDF was not found."})
                return
            attachment = parse_qs(urlparse(self.path).query).get("download", ["0"])[0] == "1"
            self._send_pdf(row["filename"], row["content"], attachment=attachment)
        elif path.startswith("/api/survey-room-test-pdfs/"):
            user = self._login_required()
            if user is None:
                return
            if not (
                _has_access_profile(user, "admin")
                or _has_access_profile(user, "surveyor")
                or _has_access_profile(user, "assign")
            ):
                self._send_json(
                    403,
                    {"error": "Admin, Surveyor, or Assign access is required to view test PDFs."},
                )
                return
            test_pdf_match = re.fullmatch(
                r"/api/survey-room-test-pdfs/([^/]+)(/preview)?", path
            )
            if test_pdf_match is None:
                self._send_json(404, {"error": "Test PDF was not found."})
                return
            pdf_name = test_pdf_match.group(1)
            if pdf_name not in SURVEY_ROOM_TEST_PDFS:
                self._send_json(404, {"error": "Test PDF was not found."})
                return
            pdf_path = (
                STATIC_DIR
                / "oldest-house-test"
                / "synthetic"
                / "pdfs"
                / pdf_name
            )
            try:
                content = pdf_path.read_bytes()
            except OSError:
                self._send_json(404, {"error": "Test PDF was not found."})
                return
            if test_pdf_match.group(2):
                query = parse_qs(urlparse(self.path).query)
                house_id = query.get("house_id", [""])[0].strip()
                if not house_id:
                    self._send_json(400, {"error": "A house ID is required."})
                    return
                try:
                    page_number = int(query.get("page", ["1"])[0])
                except ValueError:
                    self._send_json(400, {"error": "The PDF page number is invalid."})
                    return
                with connect_db() as db:
                    if not db.execute(
                        "SELECT 1 FROM provided_houses WHERE id = ?", (house_id,)
                    ).fetchone():
                        self._send_json(404, {"error": "House was not found."})
                        return
                    _require_pdf_editable(db, house_id)
                image, page_count = _render_pdf_page(content, page_number)
                self._send_png(image, (("X-PDF-Page-Count", str(page_count)),))
                return
            self._send_pdf(pdf_name, content)
        elif path == "/api/seai-documents":
            user = self._login_required()
            if user is None:
                return
            has_seai_document_access = (
                _has_access_profile(user, "admin")
                or _has_access_profile(user, "surveyor")
                or _has_access_profile(user, "assign")
            )
            with connect_db() as db:
                is_plumbing_supervisor = (
                    not has_seai_document_access
                    and _is_plumbing_supervisor(db, user)
                )
                is_vent_dashboard_user = (
                    not has_seai_document_access
                    and not is_plumbing_supervisor
                    and not _has_access_profile(user, "ventilation_coordinator")
                    and not _has_access_profile(user, "plumbing_coordinator")
                    and _in_department(db, user, "Ventilation")
                )
            if not (
                has_seai_document_access
                or is_vent_dashboard_user
                or is_plumbing_supervisor
            ):
                self._send_json(
                    403,
                    {"error": "SEAI PDFs are available to Administrators, Surveyors, Assign, and assigned Vent team leads or Plumbing Supervisors."},
                )
                return
            house_id = parse_qs(urlparse(self.path).query).get("house_id", [""])[0].strip()
            if not house_id:
                self._send_json(400, {"error": "A house ID is required."})
                return
            with connect_db() as db:
                if is_plumbing_supervisor and not _plumbing_supervisor_assigned(
                    db, house_id, user["id"]
                ):
                    self._send_json(404, {"error": "House was not found."})
                    return
                if is_vent_dashboard_user and not _ventilation_house_is_visible(
                    db, house_id, user["id"]
                ):
                    self._send_json(404, {"error": "House was not found."})
                    return
                document_table = db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' "
                    "AND name = 'seai_documents'"
                ).fetchone()
                if not document_table:
                    self._send_json(200, {"house_id": house_id, "documents": []})
                    return
                columns = {
                    row["name"] for row in db.execute("PRAGMA table_info(seai_documents)")
                }
                if "content" not in columns:
                    self._send_json(
                        503,
                        {"error": "SEAI PDF storage has not been initialized."},
                    )
                    return
                document_application_ids = [house_id]
                has_seai_applications = db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' "
                    "AND name = 'seai_applications'"
                ).fetchone()
                if has_seai_applications:
                    application_row = db.execute(
                        "SELECT detail_data_json FROM seai_applications "
                        "WHERE application_id = ?",
                        (house_id,),
                    ).fetchone()
                    if application_row:
                        try:
                            details = json.loads(application_row["detail_data_json"])
                        except (TypeError, ValueError):
                            details = None
                        seai_application_id = _find_seai_application_id(details)
                        if (
                            seai_application_id
                            and seai_application_id not in document_application_ids
                        ):
                            document_application_ids.append(seai_application_id)
                placeholders = ",".join("?" for _ in document_application_ids)
                not_started = _is_not_started_house(db, house_id)
                rows = db.execute(
                    f"""
                    SELECT documents.id, documents.category, documents.filename,
                           length(documents.content) AS size_bytes,
                           CASE WHEN selected.document_id IS NULL THEN 0 ELSE 1 END
                               AS is_selected_pdf,
                           selected.created_by AS saved_by,
                           selected.created_at AS saved_at
                    FROM seai_documents documents
                    LEFT JOIN selected_seai_documents selected
                        ON selected.document_id = documents.id
                        AND selected.house_id = ?
                    WHERE documents.application_id IN ({placeholders})
                        AND lower(documents.filename) LIKE '%.pdf'
                        AND documents.content IS NOT NULL
                    ORDER BY CASE category
                        WHEN 'Application' THEN 0
                        WHEN 'Survey' THEN 1
                        WHEN 'Variation' THEN 2
                        ELSE 3
                    END, filename COLLATE NOCASE
                    """,
                    (house_id, *document_application_ids),
                ).fetchall()
                if not_started:
                    rows = [
                        row for row in rows
                        if not _is_annotated_pdf_filename(row["filename"])
                    ]
                if is_vent_dashboard_user:
                    rows = [
                        row for row in rows
                        if row["is_selected_pdf"]
                        and not _is_annotated_pdf_filename(row["filename"])
                    ]
                if is_plumbing_supervisor:
                    rows = [
                        row for row in rows
                        if row["is_selected_pdf"]
                    ]
            self._send_json(
                200,
                {
                    "house_id": house_id,
                    "documents": [
                        {
                            **dict(row),
                            "is_annotated": _is_annotated_pdf_filename(row["filename"]),
                            "is_selected_pdf": bool(row["is_selected_pdf"]),
                        }
                        for row in rows
                    ],
                },
            )
        elif path.startswith("/api/seai-documents/"):
            user = self._login_required()
            if user is None:
                return
            has_seai_document_access = (
                _has_access_profile(user, "admin")
                or _has_access_profile(user, "surveyor")
                or _has_access_profile(user, "assign")
            )
            with connect_db() as db:
                is_plumbing_supervisor = (
                    not has_seai_document_access
                    and _is_plumbing_supervisor(db, user)
                )
                is_vent_dashboard_user = (
                    not has_seai_document_access
                    and not is_plumbing_supervisor
                    and not _has_access_profile(user, "ventilation_coordinator")
                    and not _has_access_profile(user, "plumbing_coordinator")
                    and _in_department(db, user, "Ventilation")
                )
            if not (
                has_seai_document_access
                or is_vent_dashboard_user
                or is_plumbing_supervisor
            ):
                self._send_json(
                    403,
                    {"error": "SEAI PDFs are available to Administrators, Surveyors, Assign, and assigned Vent team leads or Plumbing Supervisors."},
                )
                return
            document_path = path[len("/api/seai-documents/"):].split("/")
            is_preview = len(document_path) == 2 and document_path[1] == "preview"
            is_file = len(document_path) == 2 and document_path[1] == "file"
            if (
                len(document_path) != 2
                or not (is_preview or is_file)
                or not document_path[0].isdigit()
            ):
                self._send_json(404, {"error": "SEAI PDF was not found."})
                return
            with connect_db() as db:
                columns = {
                    row["name"] for row in db.execute("PRAGMA table_info(seai_documents)")
                }
                if "content" not in columns:
                    self._send_json(
                        503,
                        {"error": "SEAI PDF storage has not been initialized."},
                    )
                    return
                row = db.execute(
                    """
                    SELECT documents.application_id, documents.filename,
                           documents.content,
                           CASE WHEN selected.document_id IS NULL THEN 0 ELSE 1 END
                               AS is_selected_pdf
                    FROM seai_documents documents
                    LEFT JOIN selected_seai_documents selected
                      ON selected.document_id = documents.id
                     AND selected.house_id = documents.application_id
                    WHERE documents.id = ?
                      AND lower(documents.filename) LIKE '%.pdf'
                      AND documents.content IS NOT NULL
                    """,
                    (int(document_path[0]),),
                ).fetchone()
                if (
                    is_plumbing_supervisor
                    and (
                        row is None
                        or not row["is_selected_pdf"]
                        or not db.execute(
                            """
                            SELECT 1 FROM house_plumbing_assignments
                            WHERE house_id = ? AND supervisor_user_id = ?
                            UNION
                            SELECT 1 FROM house_role_assignments
                            WHERE house_id = ? AND role = 'supervisor' AND user_id = ?
                            """,
                                (
                                    row["application_id"],
                                    user["id"],
                                    row["application_id"],
                                    user["id"],
                                ),
                        ).fetchone()
                    )
                ):
                    row = None
                if (
                    is_vent_dashboard_user
                    and row is not None
                    and _is_annotated_pdf_filename(row["filename"])
                ):
                    row = None
                if (
                    row is not None
                    and _is_not_started_house(db, row["application_id"])
                    and _is_annotated_pdf_filename(row["filename"])
                ):
                    row = None
            if row is None:
                self._send_json(404, {"error": "SEAI PDF was not found."})
                return
            if is_vent_dashboard_user:
                with connect_db() as db:
                    is_visible = _ventilation_house_is_visible(
                        db, row["application_id"], user["id"]
                    )
                    is_selected_combined_pdf = db.execute(
                        """
                        SELECT 1 FROM selected_seai_documents
                        WHERE document_id = ? AND house_id = ?
                          AND created_by != ''
                        """,
                        (int(document_path[0]), row["application_id"]),
                    ).fetchone() is not None
                if not is_visible or not is_selected_combined_pdf:
                    self._send_json(404, {"error": "SEAI PDF was not found."})
                    return
            if is_preview:
                page_values = parse_qs(urlparse(self.path).query).get("page", ["1"])
                try:
                    page_number = int(page_values[0])
                except ValueError:
                    self._send_json(400, {"error": "A valid PDF page number is required."})
                    return
                image, page_count = _render_pdf_page(
                    row["content"], page_number
                )
                self._send_png(
                    image, (("X-PDF-Page-Count", str(page_count)),)
                )
            else:
                self._send_pdf(row["filename"], row["content"])
        elif path == "/api/house-vent-documents" or path.startswith("/api/house-vent-documents/"):
            user = self._login_required()
            if user is None:
                return
            can_view_all_shared_documents = (
                _has_access_profile(user, "admin")
                or _has_access_profile(user, "surveyor")
                or _has_access_profile(user, "assign")
            )
            is_ventilation_user = False
            is_ewi_supervisor = False
            with connect_db() as db:
                is_ventilation_user = _in_department(db, user, "Ventilation")
                is_ewi_supervisor = _is_ewi_supervisor(db, user)
                if (
                    not can_view_all_shared_documents
                    and not is_ventilation_user
                    and not is_ewi_supervisor
                ):
                    self._send_json(403, {"error": "You cannot view documents shared with the Vent team."})
                    return
                if path == "/api/house-vent-documents":
                    house_id = parse_qs(urlparse(self.path).query).get("house_id", [""])[0].strip()
                    if not house_id:
                        self._send_json(400, {"error": "A house ID is required."})
                        return
                    if is_ventilation_user and not can_view_all_shared_documents:
                        eligible = db.execute(
                            "SELECT 1 FROM provided_houses WHERE id = ? AND plan_vent_required = 1",
                            (house_id,),
                        ).fetchone()
                        if eligible is None:
                            self._send_json(404, {"error": "House was not found."})
                            return
                    if is_ewi_supervisor and not can_view_all_shared_documents:
                        assigned = db.execute(
                            """
                            SELECT 1 FROM house_role_assignments
                            WHERE house_id = ? AND role = 'supervisor' AND user_id = ?
                            """,
                            (house_id, user["id"]),
                        ).fetchone()
                        if assigned is None:
                            self._send_json(404, {"error": "House was not found."})
                            return
                    rows = db.execute(
                        """
                        SELECT documents.id, documents.filename, documents.size_bytes,
                               documents.shared_by, documents.shared_at,
                               (
                                   (
                                       annotations.source_document_id IS NOT NULL
                                       OR annotations.source_seai_document_id IS NOT NULL
                                   )
                                   AND annotations.annotations_json IS NOT NULL
                               ) AS annotations_editable
                        FROM house_vent_documents documents
                        LEFT JOIN house_vent_document_annotations annotations
                            ON annotations.annotated_document_id = documents.id
                        WHERE documents.house_id = ? ORDER BY documents.id DESC
                        """,
                        (house_id,),
                    ).fetchall()
                    self._send_json(200, {"house_id": house_id, "documents": [dict(row) for row in rows]})
                    return
                document_id = path[len("/api/house-vent-documents/"):]
                document_match = re.fullmatch(r"(\d+)/(file|preview|annotations)", document_id)
                if document_match is None:
                    self._send_json(404, {"error": "Document was not found."})
                    return
                row = db.execute(
                    """
                    SELECT documents.house_id, documents.filename, documents.content,
                           houses.plan_vent_required
                    FROM house_vent_documents documents
                    LEFT JOIN provided_houses houses ON houses.id = documents.house_id
                    WHERE documents.id = ?
                    """,
                    (int(document_match.group(1)),),
                ).fetchone()
                if row is None:
                    self._send_json(404, {"error": "Document was not found."})
                    return
                if (
                    is_ventilation_user
                    and not can_view_all_shared_documents
                    and row["plan_vent_required"] != 1
                ):
                    self._send_json(404, {"error": "Document was not found."})
                    return
                if is_ewi_supervisor and not can_view_all_shared_documents:
                    assigned = db.execute(
                        """
                        SELECT 1 FROM house_role_assignments
                        WHERE house_id = ? AND role = 'supervisor' AND user_id = ?
                        """,
                        (row["house_id"], user["id"]),
                    ).fetchone()
                    if assigned is None:
                        self._send_json(404, {"error": "Document was not found."})
                        return
                if document_match.group(2) == "annotations":
                    if not is_ventilation_user:
                        self._send_json(
                            403,
                            {"error": "Ventilation department access is required to edit annotated PDFs."},
                        )
                        return
                    has_seai_document_table = db.execute(
                        "SELECT 1 FROM sqlite_master WHERE type = 'table' "
                        "AND name = 'seai_documents'"
                    ).fetchone()
                    if has_seai_document_table:
                        edit_record = db.execute(
                            """
                            SELECT edits.source_document_id, edits.annotations_json,
                                   edits.source_seai_document_id
                            FROM house_vent_document_annotations edits
                            LEFT JOIN house_vent_documents source
                                ON source.id = edits.source_document_id
                            LEFT JOIN seai_documents seai_source
                                ON seai_source.id = edits.source_seai_document_id
                            WHERE edits.annotated_document_id = ?
                              AND (
                                  source.house_id = ?
                                  OR seai_source.application_id = ?
                              )
                            """,
                            (
                                int(document_match.group(1)),
                                row["house_id"],
                                row["house_id"],
                            ),
                        ).fetchone()
                    else:
                        edit_record = db.execute(
                            """
                            SELECT edits.source_document_id, edits.annotations_json,
                                   edits.source_seai_document_id
                            FROM house_vent_document_annotations edits
                            JOIN house_vent_documents source
                                ON source.id = edits.source_document_id
                            WHERE edits.annotated_document_id = ?
                              AND source.house_id = ?
                            """,
                            (int(document_match.group(1)), row["house_id"]),
                        ).fetchone()
                    if edit_record is None:
                        self._send_json(404, {"error": "Editable annotations were not found."})
                        return
                    self._send_json(
                        200,
                        {
                            "source_document_id": (
                                edit_record["source_seai_document_id"]
                                if edit_record["source_seai_document_id"] is not None
                                else edit_record["source_document_id"]
                            ),
                            "source_type": (
                                "seai"
                                if edit_record["source_seai_document_id"] is not None
                                else "vent"
                            ),
                            "annotations": json.loads(edit_record["annotations_json"]),
                        },
                    )
                    return
            if document_match.group(2) == "preview":
                page_values = parse_qs(urlparse(self.path).query).get("page", ["1"])
                try:
                    page_number = int(page_values[0])
                except ValueError:
                    self._send_json(400, {"error": "A valid PDF page number is required."})
                    return
                image, page_count = _render_pdf_page(row["content"], page_number)
                self._send_png(image, (("X-PDF-Page-Count", str(page_count)),))
            else:
                self._send_pdf(row["filename"], row["content"])
        elif path.startswith("/api/house-plumbing-photos/"):
            user = self._login_required()
            if user is None:
                return
            with connect_db() as db:
                photo_path = path[len("/api/house-plumbing-photos/"):].split("/")
                if (
                    len(photo_path) != 2
                    or not photo_path[0].isdigit()
                    or photo_path[1] != "file"
                ):
                    self._send_json(404, {"error": "Plumbing photo was not found."})
                    return
                row = db.execute(
                    """
                    SELECT house_id, filename, mime_type, content
                    FROM house_plumbing_photos
                    WHERE id = ?
                    """,
                    (int(photo_path[0]),),
                ).fetchone()
                if _has_access_profile(user, "plumbing_coordinator"):
                    can_view = bool(
                        row
                        and _plumbing_coordinator_house_is_visible(
                            db, row["house_id"]
                        )
                    )
                else:
                    can_view = _can_view_plumbing_house_photos(db, user)
                if not can_view:
                    self._send_json(403, {"error": "Plumbing photo access is required to view these images."})
                    return
                if (
                    row is not None
                    and _is_plumbing_supervisor(db, user)
                    and not _has_plumbing_photo_view_access(db, user)
                    and not _plumbing_supervisor_assigned(
                        db, row["house_id"], user["id"]
                    )
                ):
                    row = None
            if row is None:
                self._send_json(404, {"error": "Plumbing photo was not found."})
                return
            self._send_house_photo(row["filename"], row["content"], row["mime_type"])
        elif path.startswith("/api/house-vent-photos/"):
            user = self._login_required()
            if user is None:
                return
            if not _can_view_vent_house_photos(user):
                self._send_json(403, {"error": "Vent photo access is required to view these images."})
                return
            photo_path = path[len("/api/house-vent-photos/"):].split("/")
            if (
                len(photo_path) != 2
                or not photo_path[0].isdigit()
                or photo_path[1] != "file"
            ):
                self._send_json(404, {"error": "Vent photo was not found."})
                return
            with connect_db() as db:
                row = db.execute(
                    """
                    SELECT house_id, filename, mime_type, content
                    FROM house_vent_photos
                    WHERE id = ?
                    """,
                    (int(photo_path[0]),),
                ).fetchone()
                if (
                    row is not None
                    and _is_vent_photo_uploader(user)
                    and not _ventilation_house_is_visible(db, row["house_id"], user["id"])
                ):
                    row = None
                if (
                    row is not None
                    and _has_access_profile(user, "ventilation_coordinator")
                    and not _ventilation_coordinator_house_is_visible(
                        db, row["house_id"]
                    )
                ):
                    row = None
            if row is None:
                self._send_json(404, {"error": "Vent photo was not found."})
                return
            self._send_house_photo(row["filename"], row["content"], row["mime_type"])
        elif path.startswith("/api/house-documents/"):
            user = self._login_required()
            if user is None:
                return
            is_assign = _has_access_profile(user, "assign")
            is_plumbing_supervisor = False
            is_ewi_supervisor = False
            with connect_db() as db:
                is_plumbing_supervisor = _is_plumbing_supervisor(db, user)
                is_ewi_supervisor = _is_ewi_supervisor(db, user)
            has_house_document_access = (
                _has_access_profile(user, "admin")
                or _has_access_profile(user, "surveyor")
                or is_assign
                or is_plumbing_supervisor
                or is_ewi_supervisor
            )
            if not has_house_document_access:
                self._send_json(403, {"error": "Administrator, Surveyor, Assign, or assigned Supervisor access is required to view house documents."})
                return
            document_path = path[len("/api/house-documents/"):].split("/")
            is_share_pdf = len(document_path) == 2 and document_path[1] == "share"
            is_preview_page = len(document_path) == 2 and document_path[1] == "preview"
            if (is_assign or is_plumbing_supervisor or is_ewi_supervisor) and is_share_pdf:
                self._send_json(403, {"error": "This account can view but cannot share house PDFs."})
                return
            if len(document_path) not in {1, 2} or (
                len(document_path) == 2 and not (is_share_pdf or is_preview_page)
            ):
                self._send_json(404, {"error": "Document was not found."})
                return
            raw_document_id = document_path[0]
            if not raw_document_id.isdigit():
                self._send_json(404, {"error": "Document was not found."})
                return
            with connect_db() as db:
                row = db.execute(
                    """
                    SELECT d.filename, d.content, d.house_id, hs.data AS survey_data
                    FROM house_documents d
                    JOIN provided_houses p ON p.id = d.house_id
                    LEFT JOIN houses h ON h.id = p.id
                    LEFT JOIN house_surveys hs ON hs.house_id = p.id
                    WHERE d.id = ?
                      AND (? = 1 OR h.id IS NULL OR p.ready_for_start = 1)
                      AND (
                          ? = 0 OR EXISTS (
                              SELECT 1 FROM house_plumbing_assignments assignment
                              WHERE assignment.house_id = d.house_id
                                AND assignment.supervisor_user_id = ?
                          ) OR (
                              ? = 1 AND EXISTS (
                                  SELECT 1 FROM house_role_assignments assignment
                                  WHERE assignment.house_id = d.house_id
                                    AND assignment.role = 'supervisor'
                                    AND assignment.user_id = ?
                              )
                          )
                      )
                    """,
                    (
                        int(raw_document_id),
                        int(
                            is_assign
                            or is_plumbing_supervisor
                            or is_ewi_supervisor
                        ),
                        int(is_plumbing_supervisor or is_ewi_supervisor),
                        user["id"],
                        int(is_plumbing_supervisor or is_ewi_supervisor),
                        user["id"],
                    ),
                ).fetchone()
                not_started = (
                    row is not None and _is_not_started_house(db, row["house_id"])
                )
                if (
                    not_started
                    and row is not None
                    and _is_annotated_pdf_filename(row["filename"])
                ):
                    row = None
            if row is None:
                self._send_json(404, {"error": "Document was not found."})
                return
            if is_share_pdf and not_started:
                self._send_json(
                    409,
                    {"error": "PDFs are view-only while the house is Not started."},
                )
                return
            if is_preview_page:
                try:
                    page_number = int(parse_qs(urlparse(self.path).query).get("page", ["1"])[0])
                except ValueError:
                    self._send_json(400, {"error": "PDF page number must be a positive integer."})
                    return
                try:
                    page_image, page_count = _render_pdf_page(
                        row["content"], page_number
                    )
                except ApiError as exc:
                    self._send_json(exc.status, {"error": str(exc)})
                    return
                self._send_png(page_image, (("X-PDF-Page-Count", str(page_count)),))
                return
            if not is_share_pdf:
                self._send_pdf(row["filename"], row["content"])
                return
            include_survey_note = (
                parse_qs(urlparse(self.path).query)
                .get("include_survey_note", ["1"])[0]
                != "0"
            )
            if not include_survey_note:
                try:
                    share_pdf = _create_house_document_share_pdf(
                        row["content"], row["filename"], row["house_id"], None
                    )
                except ApiError as exc:
                    self._send_json(exc.status, {"error": str(exc)})
                    return
                share_filename = f"{row['house_id']}-{Path(row['filename']).name}"
                self._send_pdf(share_filename, share_pdf, attachment=True)
                return
            survey = json.loads(row["survey_data"]) if row["survey_data"] else {}
            survey_note = str(survey.get("notes") or "").strip()
            if not survey_note:
                self._send_json(
                    409,
                    {"error": "Save a survey note before sharing the PDF with notes."},
                )
                return
            try:
                combined_pdf = _create_house_document_share_pdf(
                    row["content"],
                    row["filename"],
                    row["house_id"],
                    survey_note,
                )
            except ApiError as exc:
                self._send_json(exc.status, {"error": str(exc)})
                return
            share_filename = (
                f"{row['house_id']}-{Path(row['filename']).stem}-with-survey-note.pdf"
            )
            self._send_pdf(share_filename, combined_pdf, attachment=True)
        elif path == "/api/calculator/requests":
            user = self._login_required()
            if user is None:
                return
            house_id = parse_qs(urlparse(self.path).query).get("house_id", [""])[0].strip()
            with connect_db() as db:
                rows = db.execute(
                    """
                    SELECT id, house_id, created_at, created_by, notes, data
                    FROM material_requests WHERE house_id = ? ORDER BY id DESC LIMIT 50
                    """,
                    (house_id,),
                ).fetchall()
                delivered = _delivered_to_house(db, house_id)
                next_delivery_number = _next_delivery_number(db, house_id) if house_id else 1
            self._send_json(
                200,
                {
                    "requests": [{**dict(r), "data": json.loads(r["data"])} for r in rows],
                    "delivered": delivered,
                    "next_delivery_number": next_delivery_number,
                },
            )
        elif path == "/brusna-logo.jpg":
            self._send_file(STATIC_DIR / "brusna-logo.jpg", "image/jpeg")
        elif path == "/tommy.jpeg":
            self._send_file(ROOT / "tommy.jpeg", "image/jpeg")
        elif path == "/api/auth/status":
            user = self._current_user()
            with connect_db() as db:
                setup_required = _user_count(db) == 0
            self._send_json(
                200,
                {
                    "setup_required": setup_required,
                    "user": _public_user(user) if user else None,
                },
            )
        elif path == "/api/updates":
            if self._login_required() is None:
                return
            self._send_json(200, {"revisions": _change_revision_snapshot()})
        elif path == "/api/dashboard":
            user = self._login_required()
            if user is None:
                return
            with connect_db() as db:
                db.execute("BEGIN")
                if _has_access_profile(user, "ventilation_coordinator"):
                    ventilation = _ventilation_coordinator_dashboard(db)
                    dashboard = {
                        "materials": [],
                        "team_rosters": [],
                        "demo_mode": False,
                        "houses": 0,
                        "house_summary": {
                            "provided": len(
                                ventilation["ventilation_coordinator_houses"]
                            ),
                            "active": 0,
                            "closed": 0,
                            "ready": 0,
                            "not_started": 0,
                        },
                        "movements": [],
                        "teams": [],
                        "team_house_counts": {},
                        "house_list": [],
                        "locations": [],
                        **ventilation,
                    }
                elif _has_access_profile(user, "plumbing_coordinator"):
                    plumbing = _plumbing_coordinator_dashboard(db)
                    dashboard = {
                        "materials": [],
                        "team_rosters": [],
                        "demo_mode": False,
                        "houses": 0,
                        "house_summary": {
                            "provided": len(plumbing["plumbing_houses"]),
                            "active": 0,
                            "closed": 0,
                            "ready": 0,
                            "not_started": 0,
                        },
                        "movements": [],
                        "teams": [],
                        "team_house_counts": {},
                        "house_list": [],
                        "locations": [],
                        **plumbing,
                    }
                else:
                    dashboard = _dashboard(db)
                if any(
                    _has_access_profile(user, profile)
                    for profile in ("admin", "assign", "surveyor")
                ):
                    _attach_seai_application_summaries(
                        db, dashboard["house_list"]
                    )
                if _has_access_profile(user, "assign"):
                    dashboard["surveyor_workload"] = _surveyor_workload_summary(db)
                else:
                    for house in dashboard["house_list"]:
                        for field in (
                            "assigned_surveyor_name",
                            "assigned_surveyor_is_active",
                            "surveyor_assigned_by",
                            "surveyor_assigned_at",
                            "has_assigned_surveyor",
                        ):
                            house.pop(field, None)
                if (
                    not _has_access_profile(user, "plumbing_coordinator")
                    and not _has_access_profile(user, "ventilation_coordinator")
                    and _in_department(db, user, "Ventilation")
                ):
                    dashboard["ventilation_houses"] = _ventilation_house_list(
                        db,
                        user["id"],
                        include_photos=_can_view_vent_house_photos(user),
                    )
            if (
                not _has_access_profile(user, "ventilation_coordinator")
                and
                not _has_access_profile(user, "plumbing_coordinator")
                and _has_access_profile(user, "assign")
            ):
                dashboard["materials"] = []
                dashboard["movements"] = []
                dashboard["teams"] = []
                dashboard["team_rosters"] = []
                dashboard["locations"] = [
                    location
                    for location in dashboard["locations"]
                    if not location.startswith("warehouse")
                ]
                for house in dashboard["house_list"]:
                    house.pop("last_movement", None)
                    house["activity_history"] = [
                        entry
                        for entry in house.get("activity_history", [])
                        if entry.get("type") not in {"movement", "material_request"}
                    ]
            elif (
                not _has_access_profile(user, "ventilation_coordinator")
                and
                not _has_access_profile(user, "plumbing_coordinator")
                and _has_access_profile(user, "supervisor")
            ):
                with connect_db() as db:
                    supervisor_assignments = {
                        row["house_id"]: dict(row)
                        for row in db.execute(
                            """
                            SELECT assignment.house_id, assignment.assigned_by,
                                   assignment.assigned_at
                            FROM house_role_assignments assignment
                            JOIN provided_houses provided
                              ON provided.id = assignment.house_id
                            JOIN app_users assigned_user
                              ON assigned_user.id = assignment.user_id
                            WHERE assignment.role = 'supervisor'
                              AND assignment.user_id = ?
                              AND provided.ready_for_start = 0
                              AND assigned_user.is_active = 1
                              AND EXISTS (
                                  SELECT 1 FROM app_role_permissions access
                                  WHERE access.role_key = assigned_user.role
                                    AND access.permission_key = 'access.supervisor'
                                    AND access.is_allowed = 1
                              )
                            UNION
                            SELECT assignment.house_id, assignment.assigned_by,
                                     assignment.assigned_at
                            FROM house_plumbing_assignments assignment
                            JOIN provided_houses provided
                              ON provided.id = assignment.house_id
                            JOIN app_users assigned_user
                              ON assigned_user.id = assignment.supervisor_user_id
                            WHERE assignment.supervisor_user_id = ?
                              AND provided.ready_for_start = 0
                              AND assigned_user.is_active = 1
                            UNION
                            SELECT provided.id AS house_id, '' AS assigned_by,
                                   '' AS assigned_at
                            FROM provided_houses provided
                            WHERE provided.plan_plumber_required = 1
                              AND provided.ready_for_start = 0
                              AND provided.plan_plumber_done_at != ''
                              AND provided.plan_plumber_done_by_user_id = ?
                              AND NOT EXISTS (
                                  SELECT 1 FROM house_plumbing_assignments assignment
                                  WHERE assignment.house_id = provided.id
                              )
                            """,
                            (user["id"], user["id"], user["id"]),
                        )
                    }
                    assigned_house_ids = set(supervisor_assignments)
                    teamlead_names = [
                        row["display_name"]
                        for row in db.execute(
                            """
                            SELECT users.display_name FROM app_users users
                            JOIN user_reporting_lines reports
                              ON reports.report_user_id = users.id
                            WHERE users.is_active = 1
                              AND reports.manager_user_id = ?
                              AND EXISTS (
                                  SELECT 1 FROM app_role_permissions access
                                  WHERE access.role_key = users.role
                                    AND access.permission_key = 'access.teamlead'
                                    AND access.is_allowed = 1
                              )
                            ORDER BY display_name COLLATE NOCASE
                            """,
                            (user["id"],),
                        )
                    ]
                    ewi_teamleads = [
                        {"id": row["id"], "name": row["display_name"]}
                        for row in db.execute(
                            """
                            SELECT users.id, users.display_name
                            FROM app_users users
                            JOIN user_reporting_lines reports
                              ON reports.report_user_id = users.id
                            JOIN user_departments memberships
                              ON memberships.user_id = users.id
                            JOIN departments
                              ON departments.id = memberships.department_id
                            WHERE users.is_active = 1
                              AND reports.manager_user_id = ?
                              AND departments.name = 'External Wall Insulation' COLLATE NOCASE
                              AND departments.is_active = 1
                              AND EXISTS (
                                  SELECT 1 FROM app_role_permissions access
                                  WHERE access.role_key = users.role
                                    AND access.permission_key = 'access.teamlead'
                                    AND access.is_allowed = 1
                              )
                            ORDER BY users.display_name COLLATE NOCASE
                            """,
                            (user["id"],),
                        )
                    ]
                    managed_teamlead_ids = {
                        row["report_user_id"]
                        for row in db.execute(
                            """
                            SELECT lines.report_user_id
                            FROM user_reporting_lines lines
                            JOIN app_users reports ON reports.id = lines.report_user_id
                            JOIN app_role_permissions access
                              ON access.role_key = reports.role
                            WHERE lines.manager_user_id = ?
                              AND reports.is_active = 1
                              AND access.permission_key = 'access.teamlead'
                              AND access.is_allowed = 1
                            """,
                            (user["id"],),
                        )
                    }
                    plumbing_team_members = {}
                    is_plumbing_supervisor = _is_plumbing_supervisor(db, user)
                    is_ewi_supervisor = _is_ewi_supervisor(db, user)
                    if is_plumbing_supervisor and managed_teamlead_ids:
                        teamlead_placeholders = ",".join(
                            "?" for _ in managed_teamlead_ids
                        )
                        for row in db.execute(
                            f"""
                            SELECT lines.manager_user_id AS teamlead_id,
                                   reports.id AS member_id,
                                   reports.display_name AS member_name
                            FROM user_reporting_lines lines
                            JOIN app_users reports
                              ON reports.id = lines.report_user_id
                            JOIN app_role_permissions access
                              ON access.role_key = reports.role
                             AND access.permission_key = 'access.plumber'
                             AND access.is_allowed = 1
                            JOIN user_departments memberships
                              ON memberships.user_id = reports.id
                            JOIN departments
                              ON departments.id = memberships.department_id
                             AND departments.name = 'Plumbing' COLLATE NOCASE
                            WHERE lines.manager_user_id IN ({teamlead_placeholders})
                              AND reports.is_active = 1
                            ORDER BY reports.display_name COLLATE NOCASE
                            """,
                            tuple(managed_teamlead_ids),
                        ):
                            plumbing_team_members.setdefault(
                                row["teamlead_id"], []
                            ).append(
                                {
                                    "id": row["member_id"],
                                    "name": row["member_name"],
                                    "role": "Plumber",
                                }
                            )
                dashboard["house_list"] = [
                    {
                        **house,
                        "supervisor_assigned_by": supervisor_assignments[
                            house["id"]
                        ]["assigned_by"],
                        "supervisor_assigned_at": supervisor_assignments[
                            house["id"]
                        ]["assigned_at"],
                    }
                    for house in dashboard["house_list"]
                    if house["id"] in assigned_house_ids
                ]
                if is_ewi_supervisor:
                    with connect_db() as ewi_db:
                        _attach_seai_application_summaries(
                            ewi_db, dashboard["house_list"]
                        )
                if is_plumbing_supervisor:
                    with connect_db() as plumbing_db:
                        assigned_before_activation = plumbing_db.execute(
                            """
                            SELECT p.id, p.postcode, p.address, p.owner_name,
                                   COALESCE(p.plan_plumber_notes, '') AS plumbing_notes,
                                   CASE WHEN COALESCE(p.plan_plumber_done_at, '') != ''
                                        THEN 'done'
                                        ELSE COALESCE(
                                            hc.plumber_status, p.plan_plumber, 'not_done'
                                        )
                                   END AS plumber_status,
                                   COALESCE(p.plan_plumber_done_by, '') AS plumber_done_by,
                                   COALESCE(p.plan_plumber_done_at, '') AS plumber_done_at,
                                   p.plan_plumber_done_by_user_id,
                                   COALESCE(surveyor.display_name, '') AS surveyor_name,
                                   COALESCE(
                                       assignment.assigned_by,
                                       legacy_supervisor_assignment.assigned_by,
                                       ''
                                   )
                                       AS supervisor_assigned_by,
                                   COALESCE(
                                       assignment.assigned_at,
                                       legacy_supervisor_assignment.assigned_at,
                                       ''
                                   )
                                       AS supervisor_assigned_at,
                                   CASE WHEN p.ready_for_start = 1 THEN 'ready'
                                        ELSE 'not_started' END AS status
                            FROM provided_houses p
                            LEFT JOIN house_plumbing_assignments assignment
                              ON assignment.house_id = p.id
                            LEFT JOIN house_role_assignments legacy_supervisor_assignment
                              ON legacy_supervisor_assignment.house_id = p.id
                             AND legacy_supervisor_assignment.role = 'supervisor'
                            LEFT JOIN app_users assigned_supervisor
                              ON assigned_supervisor.id = COALESCE(
                                  assignment.supervisor_user_id,
                                  legacy_supervisor_assignment.user_id
                              )
                            LEFT JOIN houses h ON h.id = p.id
                            LEFT JOIN house_checklists hc ON hc.house_id = p.id
                            LEFT JOIN house_role_assignments survey_assignment
                              ON survey_assignment.house_id = p.id
                             AND survey_assignment.role = 'surveyor'
                            LEFT JOIN app_users surveyor
                              ON surveyor.id = survey_assignment.user_id
                            WHERE (
                                  COALESCE(
                                      assignment.supervisor_user_id,
                                      legacy_supervisor_assignment.user_id
                                  ) = ?
                                  OR (
                                      assignment.supervisor_user_id IS NULL
                                      AND legacy_supervisor_assignment.user_id IS NULL
                                      AND p.plan_plumber_done_by_user_id = ?
                                      AND p.plan_plumber_done_at != ''
                                  )
                              )
                              AND h.id IS NULL
                              AND p.plan_plumber_required = 1
                              AND NOT EXISTS (
                                  SELECT 1
                                  FROM house_status_history reset
                                  WHERE reset.house_id = p.id
                                    AND reset.status = 'not_started'
                                    AND reset.id = (
                                        SELECT latest.id
                                        FROM house_status_history latest
                                        WHERE latest.house_id = p.id
                                          AND latest.status = 'not_started'
                                        ORDER BY latest.changed_at DESC, latest.id DESC
                                        LIMIT 1
                                    )
                                    AND COALESCE(
                                        assignment.assigned_at,
                                        legacy_supervisor_assignment.assigned_at
                                    ) < reset.changed_at
                              )
                              AND COALESCE(
                                    hc.plumber_status, p.plan_plumber, 'not_done'
                                  ) != 'not_necessary'
                            ORDER BY p.id COLLATE NOCASE
                            """,
                            (user["id"], user["id"]),
                        ).fetchall()
                    existing_house_ids = {
                        house["id"] for house in dashboard["house_list"]
                    }
                    for row in assigned_before_activation:
                        if row["id"] in existing_house_ids:
                            continue
                        dashboard["house_list"].append(
                            {
                                "id": row["id"],
                                "postcode": row["postcode"],
                                "address": row["address"],
                                "owner_name": row["owner_name"],
                                "is_finished": False,
                                "delivery_person": "",
                                "delivery_team": "",
                                "plumber_status": row["plumber_status"],
                                "plan_plumber_required": 1,
                                "plumbing_notes": row["plumbing_notes"],
                                "plan_plumber_done_by": row["plumber_done_by"],
                                "plan_plumber_done_at": row["plumber_done_at"],
                                "plan_plumber_done_by_user_id": row[
                                    "plan_plumber_done_by_user_id"
                                ],
                                "surveyor_name": row["surveyor_name"],
                                "supervisor_assigned_by": row["supervisor_assigned_by"],
                                "supervisor_assigned_at": row["supervisor_assigned_at"],
                                "status": row["status"],
                                "plumbing_assignment_pending_activation": True,
                            }
                        )
                    if dashboard["house_list"]:
                        with connect_db() as plumbing_db:
                            _attach_seai_application_ids(
                                plumbing_db, dashboard["house_list"]
                            )
                            house_placeholders = ",".join(
                                "?" for _ in dashboard["house_list"]
                            )
                            documents_by_house = {}
                            for document in plumbing_db.execute(
                                f"""
                                SELECT id, house_id, filename, size_bytes,
                                       uploaded_by, uploaded_at
                                FROM house_documents
                                WHERE house_id IN ({house_placeholders})
                                ORDER BY uploaded_at DESC, id DESC
                                """,
                                [house["id"] for house in dashboard["house_list"]],
                            ):
                                if _is_annotated_pdf_filename(
                                    document["filename"]
                                ):
                                    continue
                                documents_by_house.setdefault(
                                    document["house_id"], []
                                ).append({**dict(document), "source": "house"})
                            seai_document_table = plumbing_db.execute(
                                "SELECT 1 FROM sqlite_master WHERE type = 'table' "
                                "AND name = 'seai_documents'"
                            ).fetchone()
                            if seai_document_table:
                                seai_columns = {
                                    row["name"]
                                    for row in plumbing_db.execute(
                                        "PRAGMA table_info(seai_documents)"
                                    )
                                }
                                if "content" in seai_columns:
                                    for document in plumbing_db.execute(
                                        f"""
                                        SELECT documents.id,
                                               documents.application_id AS house_id,
                                               documents.filename,
                                               length(documents.content) AS size_bytes,
                                               selected.created_by AS uploaded_by,
                                               selected.created_at AS uploaded_at
                                        FROM seai_documents documents
                                        JOIN selected_seai_documents selected
                                          ON selected.document_id = documents.id
                                         AND selected.house_id = documents.application_id
                                        WHERE documents.application_id IN ({house_placeholders})
                                          AND lower(documents.filename) LIKE '%.pdf'
                                          AND documents.content IS NOT NULL
                                        ORDER BY selected.created_at DESC, documents.id DESC
                                        """,
                                        [
                                            house["id"]
                                            for house in dashboard["house_list"]
                                        ],
                                    ):
                                        if (
                                            _is_not_started_house(
                                                plumbing_db, document["house_id"]
                                            )
                                            and _is_annotated_pdf_filename(
                                                document["filename"]
                                            )
                                        ):
                                            continue
                                        documents_by_house.setdefault(
                                            document["house_id"], []
                                        ).append(
                                            {**dict(document), "source": "seai"}
                                        )
                            for documents in documents_by_house.values():
                                documents.sort(
                                    key=lambda document: (
                                        document["uploaded_at"] or "",
                                        document["id"],
                                    ),
                                    reverse=True,
                                )
                            for house in dashboard["house_list"]:
                                house["survey_documents"] = documents_by_house.get(
                                    house["id"], []
                                )
                            photos_by_house = {}
                            for photo in plumbing_db.execute(
                                f"""
                                SELECT id, house_id, filename, size_bytes,
                                       uploaded_by, uploaded_at, activity
                                FROM house_plumbing_photos
                                WHERE house_id IN ({house_placeholders})
                                ORDER BY id DESC
                                """,
                                [
                                    house["id"]
                                    for house in dashboard["house_list"]
                                ],
                            ):
                                photos_by_house.setdefault(
                                    photo["house_id"], []
                                ).append(dict(photo))
                            for house in dashboard["house_list"]:
                                house["plumbing_photos"] = photos_by_house.get(
                                    house["id"], []
                                )
                                house["can_view_plumbing_photos"] = True
                                house["can_upload_plumbing_photos"] = True
                active_houses = sum(
                    not house["is_finished"]
                    and not house.get("plumbing_assignment_pending_activation")
                    for house in dashboard["house_list"]
                )
                closed_houses = sum(
                    house["is_finished"]
                    for house in dashboard["house_list"]
                )
                dashboard["houses"] = active_houses
                dashboard["house_summary"] = {
                    "provided": len(dashboard["house_list"]),
                    "active": active_houses,
                    "closed": closed_houses,
                    "ready": 0,
                    "not_started": 0,
                }
                dashboard["materials"] = []
                dashboard["movements"] = []
                dashboard["teams"] = []
                dashboard["team_house_counts"] = {}
                dashboard["locations"] = []
                dashboard["supervisor_assignment_options"] = {
                    "delivery_people": sorted(
                        {
                            "Karol",
                            "Gary Lowther",
                            *teamlead_names,
                            *(teamlead["name"] for teamlead in ewi_teamleads),
                        },
                        key=str.casefold,
                    ),
                    "teams": teamlead_names,
                    "ewi_teamleads": ewi_teamleads,
                }
                dashboard["team_rosters"] = [
                    roster
                    for roster in dashboard["team_rosters"]
                    if roster["teamlead_id"] in managed_teamlead_ids
                ]
                if is_plumbing_supervisor:
                    plumbing_crew = []
                    for roster in dashboard["team_rosters"]:
                        account_members = plumbing_team_members.get(
                            roster["teamlead_id"], []
                        )
                        account_names = {
                            member["name"].casefold() for member in account_members
                        }
                        roster_members = [
                            {
                                "id": member["id"],
                                "name": member["name"],
                                "role": "Crew member",
                            }
                            for member in roster["members"]
                            if member["name"].casefold() not in account_names
                        ]
                        plumbing_crew.append(
                            {
                                "teamlead_id": roster["teamlead_id"],
                                "teamlead_name": roster["teamlead_name"],
                                "team_name": roster["team_name"],
                                "members": [*account_members, *roster_members],
                            }
                        )
                    dashboard["plumbing_crew"] = plumbing_crew
            elif (
                not _has_access_profile(user, "plumbing_coordinator")
                and _has_access_profile(user, "teamlead")
            ):
                dashboard["team_rosters"] = [
                    roster
                    for roster in dashboard["team_rosters"]
                    if roster["teamlead_id"] == user["id"]
                ]
                with connect_db() as db:
                    dashboard["teamlead_ewi_houses"] = _teamlead_ewi_houses(
                        db, user["id"]
                    )
                    dashboard["teamlead_insulation_houses"] = (
                        _teamlead_insulation_houses(db, user["id"])
                    )
            can_view_survey_records = any(
                _has_access_profile(user, role)
                for role in ("admin", "assign", "surveyor", "supervisor")
            )
            for house in dashboard["house_list"]:
                raw_survey = house.pop("survey_data", None)
                if not can_view_survey_records:
                    house.pop("survey_notes", None)
                    continue
                try:
                    survey = json.loads(raw_survey) if raw_survey else None
                except (TypeError, json.JSONDecodeError):
                    raise ApiError(500, "Stored house survey data is invalid.")
                if survey is not None and not isinstance(survey, dict):
                    raise ApiError(500, "Stored house survey data is invalid.")
                house["survey"] = survey
            self._send_json(200, dashboard)
        elif path == "/api/weather":
            user = self._login_required()
            if user is None:
                return
            try:
                forecast = _met_eireann_county_forecast()
            except ApiError as exc:
                self._send_json(exc.status, {"error": str(exc)})
                return
            self._send_json(200, forecast)
        elif path == "/api/movements":
            user = self._login_required()
            if user is None:
                return
            query = parse_qs(urlparse(self.path).query).get("q", [""])[0]
            with connect_db() as db:
                self._send_json(
                    200,
                    {"movements": [_movement_json(row) for row in _movement_rows(db, 500, query)]},
                )
        elif path.startswith("/api/house-checklist/"):
            user = self._login_required()
            if user is None:
                return
            raw_house_id = path[len("/api/house-checklist/"):]
            if not raw_house_id or "/" in raw_house_id:
                self._send_json(404, {"error": "House was not found."})
                return
            house_id = unquote(raw_house_id).strip()
            with connect_db() as db:
                if db.execute(
                    "SELECT 1 FROM houses WHERE id = ?", (house_id,)
                ).fetchone():
                    checklist = db.execute(
                        """
                        SELECT scaffolding_status, esb_unclipped, plumber_status
                        FROM house_checklists WHERE house_id = ?
                        """,
                        (house_id,),
                    ).fetchone()
                else:
                    checklist = db.execute(
                        """
                        SELECT plan_scaffolding AS scaffolding_status,
                               plan_esb AS esb_unclipped,
                               COALESCE(hc.plumber_status, p.plan_plumber) AS plumber_status
                        FROM provided_houses p
                        LEFT JOIN house_checklists hc ON hc.house_id = p.id
                        WHERE p.id = ?
                        """,
                        (house_id,),
                    ).fetchone()
                    if checklist is None:
                        self._send_json(404, {"error": "House was not found."})
                        return
            self._send_json(
                200,
                {
                    "house_id": house_id,
                    "scaffolding_status": checklist["scaffolding_status"] if checklist else None,
                    "esb_unclipped": (
                        _esb_unclipped_status(checklist["esb_unclipped"])
                        if checklist
                        else None
                    ),
                    "plumber_status": checklist["plumber_status"] if checklist else None,
                },
            )
        elif path.startswith("/api/assign/seai-record/"):
            user = self._login_required()
            if user is None:
                return
            if not (
                _has_access_profile(user, "assign")
                or _has_access_profile(user, "admin")
            ):
                self._send_json(403, {"error": "Admin or Assign role access is required."})
                return
            raw_house_id = path[len("/api/assign/seai-record/"):]
            if not raw_house_id or "/" in raw_house_id:
                self._send_json(404, {"error": "House was not found."})
                return
            house_id = unquote(raw_house_id).strip()
            if not house_id:
                self._send_json(404, {"error": "House was not found."})
                return
            with connect_db() as db:
                house = db.execute(
                    """
                    SELECT p.ready_for_start,
                           EXISTS(SELECT 1 FROM houses h WHERE h.id = p.id) AS is_active
                    FROM provided_houses p
                    WHERE p.id = ?
                    """,
                    (house_id,),
                ).fetchone()
                if house is None:
                    house = db.execute(
                        "SELECT 0 AS ready_for_start, 1 AS is_active "
                        "FROM houses WHERE id = ?",
                        (house_id,),
                    ).fetchone()
                    if house is None:
                        self._send_json(404, {"error": "House was not found."})
                        return
                if (
                    not _has_access_profile(user, "assign")
                    and not bool(house["is_active"])
                ):
                    self._send_json(404, {"error": "House was not found."})
                    return
                application_table = db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' "
                    "AND name = 'seai_applications'"
                ).fetchone()
                application = (
                    db.execute(
                        "SELECT detail_data_json FROM seai_applications "
                        "WHERE application_id = ?",
                        (house_id,),
                    ).fetchone()
                    if application_table
                    else None
                )
                details = None
                if application:
                    try:
                        details = json.loads(application["detail_data_json"])
                    except (TypeError, json.JSONDecodeError):
                        raise ApiError(500, "Stored SEAI application details are invalid.")
                    if not isinstance(details, dict):
                        raise ApiError(500, "Stored SEAI application details are invalid.")
                variation_history = [
                    dict(row)
                    for row in db.execute(
                        """
                        SELECT measure_index, measure_name, survey_quantity,
                               previous_variation, variation, changed_by,
                               changed_by_username, changed_at
                        FROM seai_measure_variation_history
                        WHERE house_id = ?
                        ORDER BY id DESC
                        LIMIT 100
                        """,
                        (house_id,),
                    )
                ]
            self._send_json(
                200,
                {
                    "house_id": house_id,
                    "seai_details": details,
                    "variation_history": variation_history,
                    "ready_for_start": bool(house["ready_for_start"]),
                    "is_active": bool(house["is_active"]),
                },
            )
        elif path.startswith("/api/houses/"):
            user = self._login_required()
            if user is None:
                return
            is_admin = _has_access_profile(user, "admin")
            is_supervisor = _has_access_profile(user, "supervisor")
            is_plumbing_coordinator = _has_access_profile(user, "plumbing_coordinator")
            is_ventilation_coordinator = _has_access_profile(
                user, "ventilation_coordinator"
            )
            is_vent_photo_uploader = _is_vent_photo_uploader(user)
            is_plumbing_supervisor = False
            is_photo_viewer = (
                not is_admin
                and not is_supervisor
                and not is_vent_photo_uploader
                and _has_access_profile(user, "vent_photos")
            )
            with connect_db() as db:
                is_plumbing_photo_viewer = (
                    not is_admin
                    and not is_supervisor
                    and not is_vent_photo_uploader
                    and _has_plumbing_photo_view_access(db, user)
                )
                is_plumbing_supervisor = _is_plumbing_supervisor(db, user)
            if (
                not is_admin
                and not is_supervisor
                and not is_vent_photo_uploader
                and not is_photo_viewer
                and not is_plumbing_photo_viewer
                and not is_plumbing_coordinator
                and not is_ventilation_coordinator
            ):
                self._send_json(403, {"error": "Administrator or assigned Supervisor access is required."})
                return
            raw_house_id = path[len("/api/houses/"):]
            if not raw_house_id or "/" in raw_house_id:
                self._send_json(404, {"error": "House was not found."})
                return
            house_id = unquote(raw_house_id).strip()
            if not house_id:
                self._send_json(404, {"error": "House was not found."})
                return
            with connect_db() as db:
                if not is_admin:
                    if is_supervisor:
                        has_house_access = db.execute(
                            """
                            SELECT 1 FROM house_role_assignments
                            JOIN provided_houses provided
                              ON provided.id = house_role_assignments.house_id
                            WHERE house_role_assignments.house_id = ?
                              AND house_role_assignments.role = 'supervisor'
                              AND house_role_assignments.user_id = ?
                              AND provided.ready_for_start = 0
                            """,
                            (house_id, user["id"]),
                        ).fetchone() or (
                            is_plumbing_supervisor
                            and _plumbing_supervisor_assigned(
                                db, house_id, user["id"]
                            )
                        )
                    elif is_vent_photo_uploader:
                        has_house_access = _ventilation_house_is_visible(
                            db, house_id, user["id"]
                        )
                    elif is_plumbing_coordinator:
                        has_house_access = _plumbing_coordinator_house_is_visible(
                            db, house_id
                        )
                    elif is_ventilation_coordinator:
                        has_house_access = _ventilation_coordinator_house_is_visible(
                            db, house_id
                        )
                    elif is_photo_viewer:
                        has_house_access = db.execute(
                            "SELECT 1 FROM house_vent_photos WHERE house_id = ? LIMIT 1",
                            (house_id,),
                        ).fetchone()
                    elif is_plumbing_photo_viewer:
                        has_house_access = db.execute(
                            """
                            SELECT 1 FROM house_plumbing_photos
                            WHERE house_id = ? LIMIT 1
                            """,
                            (house_id,),
                        ).fetchone()
                    else:
                        has_house_access = None
                    if not has_house_access:
                        self._send_json(404, {"error": "House was not found."})
                        return
                house = db.execute(
                    """
                    SELECT p.id, COALESCE(NULLIF(p.postcode, ''), h.postcode) AS postcode,
                           COALESCE(NULLIF(p.owner_name, ''), h.owner_name) AS owner_name,
                           COALESCE(h.employee_name, '') AS employee_name,
                           p.address, p.area_m2, p.perimeter_m,
                           COALESCE(hc.scaffolding_status, p.plan_scaffolding) AS scaffolding_status,
                           COALESCE(hc.esb_unclipped, p.plan_esb) AS esb_unclipped,
                           CASE WHEN COALESCE(p.plan_plumber_done_at, '') != ''
                                THEN 'done'
                                ELSE COALESCE(hc.plumber_status, p.plan_plumber)
                           END AS plumber_status,
                           COALESCE(h.is_finished, 0) AS is_finished,
                           CASE WHEN p.ready_for_start = 1 THEN 'ready'
                                WHEN h.id IS NULL THEN 'not_started'
                                WHEN h.is_finished = 1 THEN 'closed'
                                ELSE 'active' END AS status,
                           COALESCE(p.notes, '') AS notes,
                           p.plan_scaffolding_required, p.plan_scaffolding_notes,
                           p.plan_scaffolding_done_by, p.plan_scaffolding_done_at,
                           p.plan_scaffolding_done_by_user_id,
                           p.plan_esb_required, p.plan_esb_notes,
                           p.plan_esb_done_by, p.plan_esb_done_at,
                           p.plan_esb_done_by_user_id,
                           p.plan_plumber_required, p.plan_plumber_notes,
                           p.plan_plumber_done_by, p.plan_plumber_done_at,
                           p.plan_plumber_done_by_user_id,
                           p.plan_vent_required, p.plan_vent_notes,
                           p.plan_vent_done_by, p.plan_vent_done_at,
                           p.plan_vent_done_by_user_id,
                           p.plan_ceramic_fuse_board, p.plan_solar_pv_required,
                           p.plan_pir_required, p.plan_order_variations,
                           p.plan_windows_required,
                           p.plan_ceramic_fuse_board_done_by,
                           p.plan_ceramic_fuse_board_done_at,
                           p.plan_ceramic_fuse_board_done_by_user_id,
                           p.plan_solar_pv_required_done_by,
                           p.plan_solar_pv_required_done_at,
                           p.plan_solar_pv_required_done_by_user_id,
                           p.plan_pir_required_done_by,
                           p.plan_pir_required_done_at,
                           p.plan_pir_required_done_by_user_id,
                           p.plan_order_variations_done_by,
                           p.plan_order_variations_done_at,
                           p.plan_order_variations_done_by_user_id,
                           p.plan_windows_required_done_by,
                           p.plan_windows_required_done_at,
                           p.plan_windows_required_done_by_user_id,
                           hs.data AS survey_data,
                           COALESCE(hs.notes, '') AS survey_notes,
                           COALESCE(hs.updated_by, '') AS survey_updated_by,
                           COALESCE(hs.updated_at, '') AS survey_updated_at
                    FROM provided_houses p
                    LEFT JOIN houses h ON h.id = p.id
                    LEFT JOIN house_checklists hc ON hc.house_id = p.id
                    LEFT JOIN house_surveys hs ON hs.house_id = p.id
                    WHERE p.id = ?
                    """,
                    (house_id,),
                ).fetchone()
                if house is None:
                    house = db.execute(
                        """
                        SELECT id, postcode, owner_name, employee_name, NULL AS address,
                               NULL AS area_m2, NULL AS perimeter_m,
                               hc.scaffolding_status, hc.esb_unclipped,
                               hc.plumber_status, is_finished,
                               CASE WHEN is_finished = 1 THEN 'closed' ELSE 'active' END AS status,
                               '' AS notes,
                               NULL AS plan_scaffolding_required, NULL AS plan_scaffolding_notes,
                               NULL AS plan_esb_required, NULL AS plan_esb_notes,
                               NULL AS plan_plumber_required, NULL AS plan_plumber_notes,
                               NULL AS plan_vent_required, NULL AS plan_vent_notes,
                               NULL AS plan_ceramic_fuse_board,
                               NULL AS plan_solar_pv_required,
                               NULL AS plan_pir_required,
                               NULL AS plan_order_variations,
                               NULL AS plan_windows_required,
                               NULL AS survey_data, '' AS survey_notes,
                               '' AS survey_updated_by, '' AS survey_updated_at
                        FROM houses
                        LEFT JOIN house_checklists hc ON hc.house_id = houses.id
                        WHERE id = ?
                        """,
                        (house_id,),
                    ).fetchone()
                if house is None:
                    self._send_json(404, {"error": "House was not found."})
                    return
                if is_plumbing_coordinator:
                    self._send_json(
                        200,
                        {
                            "house": {
                                "id": house["id"],
                                "postcode": house["postcode"],
                                "owner_name": house["owner_name"],
                                "address": house["address"],
                                "status": house["status"],
                                "plan_scaffolding_required": house["plan_scaffolding_required"],
                                "plan_scaffolding_notes": house["plan_scaffolding_notes"],
                                "plan_scaffolding_done_by": house["plan_scaffolding_done_by"],
                                "plan_scaffolding_done_at": house["plan_scaffolding_done_at"],
                                "plan_scaffolding_done_by_user_id": house["plan_scaffolding_done_by_user_id"],
                                "plan_esb_required": house["plan_esb_required"],
                                "plan_esb_notes": house["plan_esb_notes"],
                                "plan_esb_done_by": house["plan_esb_done_by"],
                                "plan_esb_done_at": house["plan_esb_done_at"],
                                "plan_esb_done_by_user_id": house["plan_esb_done_by_user_id"],
                                "plan_plumber_required": house["plan_plumber_required"],
                                "plan_plumber_notes": house["plan_plumber_notes"],
                                "plan_plumber_done_by": house["plan_plumber_done_by"],
                                "plan_plumber_done_at": house["plan_plumber_done_at"],
                                "plan_plumber_done_by_user_id": house["plan_plumber_done_by_user_id"],
                                "plan_vent_required": house["plan_vent_required"],
                                "plan_vent_notes": house["plan_vent_notes"],
                                "plan_vent_done_by": house["plan_vent_done_by"],
                                "plan_vent_done_at": house["plan_vent_done_at"],
                                "plan_vent_done_by_user_id": house["plan_vent_done_by_user_id"],
                                "plan_ceramic_fuse_board": house["plan_ceramic_fuse_board"],
                                "plan_ceramic_fuse_board_done_by": house["plan_ceramic_fuse_board_done_by"],
                                "plan_ceramic_fuse_board_done_at": house["plan_ceramic_fuse_board_done_at"],
                                "plan_ceramic_fuse_board_done_by_user_id": house["plan_ceramic_fuse_board_done_by_user_id"],
                                "plan_solar_pv_required": house["plan_solar_pv_required"],
                                "plan_solar_pv_required_done_by": house["plan_solar_pv_required_done_by"],
                                "plan_solar_pv_required_done_at": house["plan_solar_pv_required_done_at"],
                                "plan_solar_pv_required_done_by_user_id": house["plan_solar_pv_required_done_by_user_id"],
                                "plan_pir_required": house["plan_pir_required"],
                                "plan_pir_required_done_by": house["plan_pir_required_done_by"],
                                "plan_pir_required_done_at": house["plan_pir_required_done_at"],
                                "plan_pir_required_done_by_user_id": house["plan_pir_required_done_by_user_id"],
                                "plan_order_variations": house["plan_order_variations"],
                                "plan_order_variations_done_by": house["plan_order_variations_done_by"],
                                "plan_order_variations_done_at": house["plan_order_variations_done_at"],
                                "plan_order_variations_done_by_user_id": house["plan_order_variations_done_by_user_id"],
                                "plan_windows_required": house["plan_windows_required"],
                                "plan_windows_required_done_by": house["plan_windows_required_done_by"],
                                "plan_windows_required_done_at": house["plan_windows_required_done_at"],
                                "plan_windows_required_done_by_user_id": house["plan_windows_required_done_by_user_id"],
                                "can_view_plumbing_photos": True,
                                "photo_view_only": True,
                                "plumbing_photos": _plumbing_house_photo_metadata(
                                    db, house_id
                                ),
                            },
                            "movements": [],
                        },
                    )
                    return
                if is_photo_viewer or is_ventilation_coordinator:
                    house_data = {
                        "id": house["id"],
                        "postcode": house["postcode"],
                        "owner_name": house["owner_name"],
                        "address": house["address"],
                        "status": house["status"],
                        "can_view_vent_photos": True,
                        "photo_view_only": (
                            is_photo_viewer and not is_ventilation_coordinator
                        ),
                        "coordinator_view_only": is_ventilation_coordinator,
                        "vent_photos": _vent_house_photo_metadata(db, house_id),
                    }
                    if is_ventilation_coordinator:
                        try:
                            survey_data = json.loads(house["survey_data"] or "{}")
                        except (TypeError, ValueError) as exc:
                            raise ApiError(
                                500, "Saved survey details are invalid."
                            ) from exc
                        requirements = _attic_cavity_quantities(db, [house_id]).get(
                            house_id, {}
                        )
                        application = db.execute(
                            "SELECT detail_data_json FROM seai_applications "
                            "WHERE application_id = ?",
                            (house_id,),
                        ).fetchone()
                        try:
                            seai_details = (
                                json.loads(application["detail_data_json"])
                                if application
                                else {}
                            )
                        except (TypeError, ValueError) as exc:
                            raise ApiError(
                                500, "Stored SEAI application details are invalid."
                            ) from exc
                        house_data.update(
                            {
                                "employee_name": house["employee_name"],
                                "perimeter_m": survey_data.get("perimeter_m"),
                                "survey_perimeter_m": survey_data.get("perimeter_m"),
                                "survey_data": survey_data,
                                "survey_notes": house["survey_notes"],
                                "survey_updated_by": house["survey_updated_by"],
                                "survey_updated_at": house["survey_updated_at"],
                                "seai_details": seai_details,
                                "attic_required": "attic" in requirements,
                                "attic_survey_quantity": requirements.get(
                                    "attic", ""
                                ),
                                "cavity_required": "cavity" in requirements,
                                "cavity_survey_quantity": requirements.get(
                                    "cavity", ""
                                ),
                            }
                        )
                        work_plan_fields = [
                            f"plan_{trade}_{suffix}"
                            for trade in WORK_PLAN_TRADES
                            for suffix in (
                                "required",
                                "notes",
                                "done_by",
                                "done_at",
                                "done_by_user_id",
                            )
                        ]
                        work_plan_fields.extend(
                            field
                            for item in WORK_PLAN_SURVEY_QUESTIONS
                            for field in (
                                f"plan_{item}",
                                f"plan_{item}_done_by",
                                f"plan_{item}_done_at",
                                f"plan_{item}_done_by_user_id",
                            )
                        )
                        house_data.update(
                            {field: house[field] for field in work_plan_fields}
                        )
                    self._send_json(
                        200,
                        {
                            "house": house_data,
                            "movements": [],
                        },
                    )
                    return
                if is_plumbing_photo_viewer:
                    self._send_json(
                        200,
                        {
                            "house": {
                                "id": house["id"],
                                "postcode": house["postcode"],
                                "owner_name": house["owner_name"],
                                "address": house["address"],
                                "status": house["status"],
                                "can_view_plumbing_photos": True,
                                "photo_view_only": True,
                                "plumbing_photos": _plumbing_house_photo_metadata(
                                    db, house_id
                                ),
                            },
                            "movements": [],
                        },
                    )
                    return
                movements = _house_movement_rows(db, house_id)
                house_data = dict(house)
                try:
                    survey_data = (
                        json.loads(house_data["survey_data"])
                        if house_data.get("survey_data")
                        else {}
                    )
                except (TypeError, json.JSONDecodeError):
                    raise ApiError(500, "Stored house survey data is invalid.")
                if not isinstance(survey_data, dict):
                    raise ApiError(500, "Stored house survey data is invalid.")
                house_data["survey_perimeter_m"] = survey_data.get("perimeter_m")
                house_data.pop("survey_data", None)
                seai_application = db.execute(
                    "SELECT detail_data_json FROM seai_applications "
                    "WHERE application_id = ?",
                    (house_id,),
                ).fetchone() if db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' "
                    "AND name = 'seai_applications'"
                ).fetchone() else None
                if seai_application:
                    try:
                        house_data["seai_details"] = json.loads(
                            seai_application["detail_data_json"]
                        )
                    except (TypeError, json.JSONDecodeError):
                        raise ApiError(500, "Stored SEAI application details are invalid.")
                else:
                    house_data["seai_details"] = None
                house_data["esb_unclipped"] = _esb_unclipped_status(
                    house_data["esb_unclipped"]
                )
                house_data["snags"] = _snags_by_house(db, include_created_at=True).get(
                    house_id, []
                )
                can_view_vent_photos = _can_view_vent_house_photos(user)
                house_data["can_view_vent_photos"] = can_view_vent_photos
                house_data["vent_photos"] = (
                    _vent_house_photo_metadata(db, house_id)
                    if can_view_vent_photos
                    else []
                )
                can_view_plumbing_photos = _can_view_plumbing_house_photos(
                    db, user
                )
                house_data["can_view_plumbing_photos"] = (
                    can_view_plumbing_photos
                )
                house_data["can_upload_plumbing_photos"] = (
                    _is_plumbing_supervisor(db, user)
                    and _plumbing_supervisor_assigned(db, house_id, user["id"])
                )
                house_data["plumbing_photos"] = (
                    _plumbing_house_photo_metadata(db, house_id)
                    if can_view_plumbing_photos
                    else []
                )
                house_data.update(
                    _house_audit_by_id(db).get(
                        house_id, {"status_history": [], "last_movement": None}
                    )
                )
                self._send_json(
                    200,
                    {
                        "house": house_data,
                        "movements": [_movement_json(row) for row in movements],
                    },
                )
        elif path == "/api/users":
            user = self._login_required()
            if user is None:
                return
            if not self._admin_required(user):
                return
            with connect_db() as db:
                users = db.execute(
                    """
                    SELECT users.id, users.username, users.display_name, users.role,
                           roles.display_name AS role_name, users.is_active,
                           users.supervisor_specialty, users.team_name,
                           EXISTS (
                               SELECT 1 FROM app_user_plumbing_photo_access photo_access
                               WHERE photo_access.user_id = users.id
                           ) AS can_view_plumbing_photos,
                           COALESCE((
                               SELECT json_group_array(json_object(
                                   'id', departments.id,
                                   'name', departments.name,
                                   'is_primary', memberships.is_primary
                               ))
                               FROM user_departments memberships
                               JOIN departments ON departments.id = memberships.department_id
                               WHERE memberships.user_id = users.id
                           ), '[]') AS departments_json,
                           COALESCE((
                               SELECT group_concat(permission_key, char(31))
                               FROM app_role_permissions
                               WHERE role_key = users.role AND is_allowed = 1
                           ), '') AS role_permissions,
                           (SELECT manager_user_id FROM user_reporting_lines
                            WHERE report_user_id = users.id LIMIT 1) AS manager_user_id
                    FROM app_users users
                    LEFT JOIN app_roles roles ON roles.role_key = users.role
                    ORDER BY users.username COLLATE NOCASE
                    """
                ).fetchall()
                departments = [
                    dict(row)
                    for row in db.execute(
                        "SELECT id, name, is_active FROM departments "
                        "ORDER BY name COLLATE NOCASE"
                    )
                ]
                roles = db.execute(
                    """
                    SELECT roles.role_key, roles.display_name, roles.is_active,
                           COALESCE((
                               SELECT json_group_array(permissions.permission_key)
                               FROM app_role_permissions assignments
                               JOIN app_permissions permissions
                                 ON permissions.permission_key = assignments.permission_key
                               WHERE assignments.role_key = roles.role_key
                                 AND assignments.is_allowed = 1
                           ), '[]') AS permissions_json
                    FROM app_roles roles
                    ORDER BY roles.display_name COLLATE NOCASE
                    """
                ).fetchall()
                permission_catalog = [
                    dict(row)
                    for row in db.execute(
                        "SELECT permission_key, description FROM app_permissions "
                        "ORDER BY permission_key"
                    )
                ]
            user_rows = []
            for row in users:
                item = dict(row)
                try:
                    item["departments"] = json.loads(item.pop("departments_json"))
                except (TypeError, json.JSONDecodeError):
                    raise ApiError(500, "Stored user department data is invalid.")
                item["permissions"] = [
                    permission
                    for permission in item.pop("role_permissions", "").split("\x1f")
                    if permission
                ]
                user_rows.append(item)
            role_rows = []
            for row in roles:
                item = dict(row)
                try:
                    item["permissions"] = json.loads(item.pop("permissions_json"))
                except (TypeError, json.JSONDecodeError):
                    raise ApiError(500, "Stored role permission data is invalid.")
                role_rows.append(item)
            self._send_json(
                200,
                {
                    "users": user_rows,
                    "departments": departments,
                    "roles": role_rows,
                    "permissions": permission_catalog,
                },
            )
        elif path == "/api/departments":
            user = self._login_required()
            if user is None:
                return
            if not self._admin_required(user):
                return
            with connect_db() as db:
                departments = [
                    dict(row)
                    for row in db.execute(
                        "SELECT id, name, is_active FROM departments "
                        "ORDER BY name COLLATE NOCASE"
                    )
                ]
            self._send_json(200, {"departments": departments})
        elif path == "/api/roles":
            user = self._login_required()
            if user is None:
                return
            if not self._admin_required(user):
                return
            with connect_db() as db:
                roles = [
                    dict(row)
                    for row in db.execute(
                        """
                        SELECT roles.role_key, roles.display_name, roles.is_active,
                               COALESCE((
                                   SELECT json_group_array(assignments.permission_key)
                                   FROM app_role_permissions assignments
                                   WHERE assignments.role_key = roles.role_key
                                     AND assignments.is_allowed = 1
                               ), '[]') AS permissions_json
                        FROM app_roles roles
                        ORDER BY roles.display_name COLLATE NOCASE
                        """
                    )
                ]
                permissions = [
                    dict(row)
                    for row in db.execute(
                        "SELECT permission_key, description FROM app_permissions "
                        "ORDER BY permission_key"
                    )
                ]
            for role in roles:
                try:
                    role["permissions"] = json.loads(role.pop("permissions_json"))
                except (TypeError, json.JSONDecodeError):
                    raise ApiError(500, "Stored role permission data is invalid.")
            self._send_json(
                200,
                {"roles": roles, "permissions": permissions},
            )
        else:
            self._send_json(404, {"error": "Page not found."})

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            if path == "/api/auth/setup":
                payload = self._json_body()
                if not isinstance(payload, dict):
                    raise ApiError(400, "Administrator details must be a JSON object.")
                with connect_db() as db:
                    db.execute("BEGIN IMMEDIATE")
                    if _user_count(db):
                        raise ApiError(409, "The initial administrator has already been set up.")
                    user_id = _insert_user(
                        db,
                        payload.get("username"),
                        payload.get("display_name"),
                        payload.get("pin"),
                        "admin",
                    )
                    token, _, _ = _new_session(db, user_id)
                    user = _user_record(db, user_id)
                self._send_json(
                    201, {"user": _public_user(user)}, self._session_headers(token)
                )
                return
            if path == "/api/auth/login":
                payload = self._json_body()
                if not isinstance(payload, dict):
                    raise ApiError(400, "Sign-in details must be a JSON object.")
                username = str(payload.get("username") or "").strip()
                pin = payload.get("pin")
                username_hash = _login_attempt_key(username)
                now = int(datetime.now().timestamp())
                with connect_db() as db:
                    db.execute("BEGIN IMMEDIATE")
                    _check_login_attempts(db, username_hash, now)
                    row = db.execute(
                        "SELECT * FROM app_users WHERE username = ? COLLATE NOCASE",
                        (username,),
                    ).fetchone()
                    if not _verify_credential(row, pin) or not row["is_active"]:
                        _record_failed_login(db, username_hash, now)
                        attempts = db.execute(
                            "SELECT locked_until FROM app_login_attempts WHERE username_hash = ?",
                            (username_hash,),
                        ).fetchone()
                        locked = attempts["locked_until"] > now
                        token = None
                        user = None
                    else:
                        _clear_login_attempts(db, username_hash)
                        token, _, _ = _new_session(db, row["id"])
                        user = _public_user(_user_record(db, row["id"]))
                if user is None:
                    if locked:
                        raise ApiError(
                            429,
                            "Too many incorrect attempts. Try again in 15 minutes.",
                        )
                    raise ApiError(401, "Username or PIN is incorrect.")
                self._send_json(
                    200, {"user": user}, self._session_headers(token)
                )
                return
            if path == "/api/auth/logout":
                token = self._session_cookie()
                if token:
                    token_hash = hashlib.sha256(
                        token.encode("ascii", errors="ignore")
                    ).hexdigest()
                    with connect_db() as db:
                        db.execute(
                            "DELETE FROM app_sessions WHERE token_hash = ?",
                            (token_hash,),
                        )
                self._send_json(
                    200, {"ok": True}, self._expired_session_headers()
                )
                return
            if path == "/api/auth/pin":
                user = self._login_required()
                if user is None:
                    return
                payload = self._json_body()
                if not isinstance(payload, dict):
                    raise ApiError(400, "PIN details must be a JSON object.")
                current_pin = payload.get("current_pin")
                new_pin = _validate_pin(payload.get("new_pin"))
                with connect_db() as db:
                    row = db.execute(
                        "SELECT * FROM app_users WHERE id = ?", (user["id"],)
                    ).fetchone()
                    if not _verify_credential(row, current_pin):
                        raise ApiError(401, "Current PIN is incorrect.")
                    salt = secrets.token_bytes(16)
                    db.execute(
                        """
                        UPDATE app_users
                        SET password_salt = ?, password_hash = ?, password_iterations = ?,
                            credential_type = 'pin'
                        WHERE id = ?
                        """,
                        (
                            salt,
                            _credential_digest(new_pin, salt),
                            CREDENTIAL_ITERATIONS,
                            user["id"],
                        ),
                    )
                    db.execute(
                        "DELETE FROM app_sessions WHERE user_id = ?",
                        (user["id"],),
                    )
                    token, _, _ = _new_session(db, user["id"])
                self._send_json(
                    200,
                    {"ok": True},
                    self._session_headers(token),
                )
                return

            user = self._login_required()
            if user is None:
                return
            vent_photo_delete_match = re.fullmatch(
                r"/api/house-vent-photos/(\d+)/delete", path
            )
            if vent_photo_delete_match:
                if not _is_vent_photo_uploader(user):
                    raise ApiError(
                        403,
                        "Only the Vent photo uploader can delete attached photos.",
                    )
                photo_id = int(vent_photo_delete_match.group(1))
                with connect_db() as db:
                    with db:
                        db.execute("BEGIN IMMEDIATE")
                        photo = db.execute(
                            """
                            SELECT house_id, filename
                            FROM house_vent_photos
                            WHERE id = ?
                            """,
                            (photo_id,),
                        ).fetchone()
                        if photo is None or not _ventilation_house_is_visible(
                            db, photo["house_id"], user["id"]
                        ):
                            raise ApiError(404, "Assigned Vent photo was not found.")
                        db.execute(
                            "DELETE FROM house_vent_photos WHERE id = ?",
                            (photo_id,),
                        )
                        remaining_count = db.execute(
                            "SELECT COUNT(*) FROM house_vent_photos WHERE house_id = ?",
                            (photo["house_id"],),
                        ).fetchone()[0]
                self._send_json(
                    200,
                    {
                        "ok": True,
                        "id": photo_id,
                        "house_id": photo["house_id"],
                        "filename": photo["filename"],
                        "remaining_count": remaining_count,
                    },
                )
                return
            plumbing_photo_delete_match = re.fullmatch(
                r"/api/house-plumbing-photos/(\d+)/delete", path
            )
            if plumbing_photo_delete_match:
                photo_id = int(plumbing_photo_delete_match.group(1))
                with connect_db() as db:
                    if not _is_plumbing_supervisor(db, user):
                        raise ApiError(
                            403,
                            "Only Plumbing Supervisors can delete Plumbing team photos.",
                        )
                    with db:
                        db.execute("BEGIN IMMEDIATE")
                        photo = db.execute(
                            """
                            SELECT photo.house_id, photo.filename,
                                   COALESCE(checklist.plumber_status,
                                            provided.plan_plumber, 'not_done')
                                       AS plumber_status,
                                   (SELECT COUNT(*) FROM house_plumbing_photos count_photos
                                    WHERE count_photos.house_id = photo.house_id)
                                       AS photo_count
                            FROM house_plumbing_photos photo
                            JOIN provided_houses provided
                              ON provided.id = photo.house_id
                            LEFT JOIN house_checklists checklist
                              ON checklist.house_id = photo.house_id
                            WHERE photo.id = ?
                              AND provided.plan_plumber_required = 1
                              AND EXISTS (
                                  SELECT 1 FROM house_plumbing_assignments assignment
                                  WHERE assignment.house_id = photo.house_id
                                    AND assignment.supervisor_user_id = ?
                              )
                            """,
                            (photo_id, user["id"]),
                        ).fetchone()
                        if photo is None:
                            raise ApiError(404, "Assigned Plumbing photo was not found.")
                        db.execute(
                            "DELETE FROM house_plumbing_photos WHERE id = ?",
                            (photo_id,),
                        )
                        plumbing_reopened = (
                            photo["photo_count"] == 1
                            and photo["plumber_status"] == "done"
                        )
                        if plumbing_reopened:
                            db.execute(
                                """
                                INSERT OR IGNORE INTO house_checklists (house_id)
                                VALUES (?)
                                """,
                                (photo["house_id"],),
                            )
                            db.execute(
                                """
                                UPDATE house_checklists
                                SET plumber_status = 'not_done'
                                WHERE house_id = ?
                                """,
                                (photo["house_id"],),
                            )
                            db.execute(
                                """
                                UPDATE provided_houses
                                SET plan_plumber_done_by = '',
                                    plan_plumber_done_at = '',
                                    plan_plumber_done_by_user_id = NULL
                                WHERE id = ?
                                """,
                                (photo["house_id"],),
                            )
                        remaining_count = db.execute(
                            "SELECT COUNT(*) FROM house_plumbing_photos WHERE house_id = ?",
                            (photo["house_id"],),
                        ).fetchone()[0]
                self._send_json(
                    200,
                    {
                        "ok": True,
                        "id": photo_id,
                        "house_id": photo["house_id"],
                        "filename": photo["filename"],
                        "remaining_count": remaining_count,
                        "plumbing_reopened": plumbing_reopened,
                    },
                )
                return
            house_document_delete_match = re.fullmatch(
                r"/api/house-documents/(\d+)/delete", path
            )
            if house_document_delete_match:
                if not (_has_access_profile(user, "admin") or _has_access_profile(user, "surveyor")):
                    raise ApiError(
                        403,
                        "Administrator or surveyor access is required to delete house PDFs.",
                    )
                document_id = int(house_document_delete_match.group(1))
                with connect_db() as db:
                    with db:
                        db.execute("BEGIN IMMEDIATE")
                        document = db.execute(
                            """
                            SELECT d.filename
                            FROM house_documents d
                            JOIN provided_houses p ON p.id = d.house_id
                            LEFT JOIN houses h ON h.id = p.id
                            WHERE d.id = ? AND p.ready_for_start = 1
                              AND h.id IS NULL
                            """,
                            (document_id,),
                        ).fetchone()
                        if document is None:
                            exists = db.execute(
                                "SELECT 1 FROM house_documents WHERE id = ?",
                                (document_id,),
                            ).fetchone()
                            if exists is None:
                                raise ApiError(404, "House PDF was not found.")
                            raise ApiError(
                                409,
                                "House PDFs can only be deleted while the house is Ready for start and inactive.",
                            )
                        db.execute(
                            "DELETE FROM house_documents WHERE id = ?",
                            (document_id,),
                        )
                self._send_json(
                    200,
                    {"ok": True, "filename": document["filename"]},
                )
                return
            ewi_share_match = re.fullmatch(
                r"/api/seai-documents/(\d+)/share-with-ewi-team", path
            )
            if ewi_share_match:
                if not (
                    _has_access_profile(user, "admin")
                    or _has_access_profile(user, "surveyor")
                ):
                    raise ApiError(
                        403,
                        "Administrator or surveyor access is required to share annotated PDFs with the External Insulation team.",
                    )
                document_id = int(ewi_share_match.group(1))
                with connect_db() as db:
                    document_table = db.execute(
                        "SELECT 1 FROM sqlite_master WHERE type = 'table' "
                        "AND name = 'seai_documents'"
                    ).fetchone()
                    if not document_table:
                        raise ApiError(404, "Annotated SEAI PDF was not found.")
                    columns = {
                        row["name"]
                        for row in db.execute("PRAGMA table_info(seai_documents)")
                    }
                    if "content" not in columns:
                        raise ApiError(503, "SEAI PDF storage has not been initialized.")
                    source = db.execute(
                        """
                        SELECT documents.id, documents.application_id, documents.filename
                        FROM seai_documents documents
                        JOIN provided_houses houses ON houses.id = documents.application_id
                        WHERE documents.id = ? AND lower(documents.filename) LIKE '%.pdf'
                          AND documents.content IS NOT NULL
                        """,
                        (document_id,),
                    ).fetchone()
                    if source is None or not _is_annotated_pdf_filename(source["filename"]):
                        raise ApiError(404, "Annotated SEAI PDF was not found.")
                    _require_pdf_editable(db, source["application_id"])
                    recipients = _external_insulation_supervisors(db)
                    if not recipients:
                        raise ApiError(404, "No active External Insulation supervisor accounts were found.")
                    shared_at = datetime.now().isoformat(timespec="seconds")
                    inserted = sum(
                        db.execute(
                            """
                            INSERT OR IGNORE INTO seai_document_shares
                                (document_id, house_id, recipient_user_id, shared_by, shared_at)
                            VALUES (?, ?, ?, ?, ?)
                            """,
                            (
                                document_id,
                                source["application_id"],
                                recipient["id"],
                                user["display_name"],
                                shared_at,
                            ),
                        ).rowcount
                        for recipient in recipients
                    )
                self._send_json(
                    200,
                    {
                        "shared": bool(inserted),
                        "recipient_count": len(recipients),
                        "house_id": source["application_id"],
                        "filename": source["filename"],
                    },
                )
                return
            seai_share_match = re.fullmatch(r"/api/seai-documents/(\d+)/share", path)
            if seai_share_match:
                if not (_has_access_profile(user, "admin") or _has_access_profile(user, "surveyor")):
                    raise ApiError(
                        403,
                        "Administrator or surveyor access is required to share SEAI PDFs.",
                    )
                include_survey_note = (
                    parse_qs(urlparse(self.path).query)
                    .get("include_survey_note", ["1"])[0]
                    != "0"
                )
                document_id = int(seai_share_match.group(1))
                with connect_db() as db:
                    source = db.execute(
                        """
                        SELECT id, application_id, filename, content
                        FROM seai_documents
                        WHERE id = ? AND lower(filename) LIKE '%.pdf'
                            AND content IS NOT NULL
                        """,
                        (document_id,),
                    ).fetchone()
                    if source is None:
                        raise ApiError(404, "SEAI PDF was not found.")
                    _require_pdf_editable(db, source["application_id"])
                    survey_note = None
                    if include_survey_note:
                        survey_row = db.execute(
                            "SELECT data FROM house_surveys WHERE house_id = ?",
                            (source["application_id"],),
                        ).fetchone()
                        if survey_row is None:
                            raise ApiError(
                                409, "Save a survey note before sharing an SEAI PDF."
                            )
                        try:
                            survey_data = json.loads(survey_row["data"])
                        except (json.JSONDecodeError, TypeError) as exc:
                            raise ApiError(500, "The saved house survey could not be read.") from exc
                        if not isinstance(survey_data, dict):
                            raise ApiError(500, "The saved house survey could not be read.")
                        survey_note = str(survey_data.get("notes") or "").strip()
                        if not survey_note:
                            raise ApiError(
                                409, "Save a survey note before sharing an SEAI PDF."
                            )
                shared_pdf = _create_house_document_share_pdf(
                    source["content"],
                    source["filename"],
                    source["application_id"],
                    survey_note,
                )
                suffix = "-with-survey-note" if include_survey_note else ""
                share_filename = f"{source['application_id']}-{Path(source['filename']).stem}{suffix}.pdf"
                self._send_pdf(share_filename, shared_pdf, attachment=True)
                return
            if path == "/api/seai-documents/save-selected":
                if not (
                    _has_access_profile(user, "admin")
                    or _has_access_profile(user, "surveyor")
                ):
                    raise ApiError(
                        403,
                        "Administrator or surveyor access is required to save selected SEAI pages.",
                    )
                query = parse_qs(urlparse(self.path).query)
                house_id = query.get("house_id", [""])[0].strip()
                if not house_id:
                    raise ApiError(400, "A house ID is required.")
                content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
                if content_type != "application/pdf":
                    raise ApiError(400, "Save the selected pages as a PDF.")
                file_data = self._body()
                if not file_data.startswith(b"%PDF-"):
                    raise ApiError(400, "The selected pages are not a valid PDF.")
                if len(file_data) > SEAI_DOCUMENT_MAX_BYTES:
                    raise ApiError(413, "The selected PDF exceeds the 20 MB SEAI document limit.")
                try:
                    pdf_document = _open_pdf_document(file_data)
                except ApiError as exc:
                    raise ApiError(422, "The selected PDF could not be read.") from exc
                try:
                    page_count = pdf_document.page_count
                finally:
                    pdf_document.close()
                if not 1 <= page_count <= HOUSE_DOCUMENT_MAX_PAGES:
                    raise ApiError(400, "The selected PDF must contain between 1 and 300 pages.")
                with connect_db() as db:
                    columns = {
                        row["name"] for row in db.execute("PRAGMA table_info(seai_documents)")
                    }
                    if "content" not in columns:
                        raise ApiError(503, "SEAI PDF storage has not been initialized.")
                    _require_pdf_editable(db, house_id)
                    filename_stem = f"{house_id}-selected-seai-pages"
                    filename = f"{filename_stem}.pdf"
                    suffix = 2
                    while db.execute(
                        """
                        SELECT 1 FROM seai_documents
                        WHERE application_id = ? AND filename = ? COLLATE NOCASE
                        """,
                        (house_id, filename),
                    ).fetchone():
                        filename = f"{filename_stem}-{suffix}.pdf"
                        suffix += 1
                    with db:
                        cursor = db.execute(
                            """
                            INSERT INTO seai_documents
                                (application_id, category, filename, extension,
                                 downloaded, content)
                            VALUES (?, 'Survey', ?, '.pdf', 1, ?)
                            """,
                            (house_id, filename, sqlite3.Binary(file_data)),
                        )
                        db.execute(
                            """
                            INSERT INTO selected_seai_documents
                                (document_id, house_id, created_by, created_at)
                            VALUES (?, ?, ?, ?)
                            """,
                            (
                                cursor.lastrowid,
                                house_id,
                                user["display_name"],
                                datetime.now().isoformat(timespec="seconds"),
                            ),
                        )
                self._send_json(
                    201,
                    {
                        "id": cursor.lastrowid,
                        "house_id": house_id,
                        "category": "Survey",
                        "filename": filename,
                        "page_count": page_count,
                    },
                )
                return
            selected_seai_delete_match = re.fullmatch(
                r"/api/seai-documents/(\d+)/delete-selected", path
            )
            if selected_seai_delete_match:
                if not (
                    _has_access_profile(user, "admin")
                    or _has_access_profile(user, "surveyor")
                ):
                    raise ApiError(
                        403,
                        "Administrator or surveyor access is required to delete saved selected PDFs.",
                    )
                document_id = int(selected_seai_delete_match.group(1))
                with connect_db() as db:
                    with db:
                        document = db.execute(
                            """
                            SELECT documents.application_id, documents.filename
                            FROM seai_documents documents
                            JOIN selected_seai_documents selected
                                ON selected.document_id = documents.id
                            WHERE documents.id = ?
                            """,
                            (document_id,),
                        ).fetchone()
                        if document is None:
                            raise ApiError(404, "Saved selected PDF was not found.")
                        _require_pdf_editable(db, document["application_id"])
                        db.execute(
                            "DELETE FROM seai_document_shares WHERE document_id = ?",
                            (document_id,),
                        )
                        db.execute(
                            "DELETE FROM selected_seai_documents WHERE document_id = ?",
                            (document_id,),
                        )
                        db.execute(
                            "DELETE FROM seai_documents WHERE id = ?",
                            (document_id,),
                        )
                self._send_json(
                    200,
                    {
                        "deleted": True,
                        "house_id": document["application_id"],
                        "filename": document["filename"],
                    },
                )
                return
            if path == "/api/seai-documents/share":
                if not (_has_access_profile(user, "admin") or _has_access_profile(user, "surveyor")):
                    raise ApiError(
                        403,
                        "Administrator or surveyor access is required to share SEAI PDFs.",
                    )
                payload = self._json_body()
                if not isinstance(payload, dict):
                    raise ApiError(400, "SEAI PDF share details must be a JSON object.")
                house_id = str(payload.get("house_id") or "").strip()
                selections = payload.get("pages")
                icon_images = payload.get("icon_images")
                include_survey_note = payload.get("include_survey_note", True)
                if not isinstance(include_survey_note, bool):
                    raise ApiError(400, "The survey note option must be true or false.")
                if (
                    not house_id
                    or not isinstance(selections, list)
                    or not selections
                    or len(selections) > HOUSE_DOCUMENT_MAX_PAGES
                ):
                    raise ApiError(
                        400,
                        "Choose between 1 and 300 SEAI PDF pages to share.",
                    )
                normalized_selections = []
                selected_keys = set()
                document_ids = set()
                for selection in selections:
                    if not isinstance(selection, dict):
                        raise ApiError(400, "Selected SEAI PDF pages are invalid.")
                    if selection.get("blank_page") is True:
                        if any(
                            key in selection
                            for key in ("document_id", "test_pdf_name", "page")
                        ):
                            raise ApiError(400, "Blank PDF page details are invalid.")
                        normalized_selections.append(
                            {
                                "blank_page": True,
                                "annotations": selection.get("annotations"),
                            }
                        )
                        continue
                    test_pdf_name = selection.get("test_pdf_name")
                    if test_pdf_name is not None:
                        if "document_id" in selection:
                            raise ApiError(400, "Selected test PDF page details are invalid.")
                        if (
                            not isinstance(test_pdf_name, str)
                            or test_pdf_name not in SURVEY_ROOM_TEST_PDFS
                        ):
                            raise ApiError(404, "Test PDF was not found.")
                        page_number = selection.get("page")
                        if (
                            isinstance(page_number, bool)
                            or not isinstance(page_number, int)
                            or page_number < 1
                        ):
                            raise ApiError(
                                400, "Selected SEAI PDF pages must have valid document and page numbers."
                            )
                        page_key = ("test", test_pdf_name, page_number)
                        if page_key in selected_keys:
                            raise ApiError(400, "A SEAI PDF page was selected more than once.")
                        selected_keys.add(page_key)
                        normalized_selections.append(
                            {
                                "test_pdf_name": test_pdf_name,
                                "page": page_number,
                                "annotations": selection.get("annotations"),
                            }
                        )
                        continue
                    document_id = selection.get("document_id")
                    page_number = selection.get("page")
                    if (
                        isinstance(document_id, bool)
                        or not isinstance(document_id, int)
                        or document_id < 1
                        or isinstance(page_number, bool)
                        or not isinstance(page_number, int)
                        or page_number < 1
                    ):
                        raise ApiError(
                            400, "Selected SEAI PDF pages must have valid document and page numbers."
                        )
                    page_key = (document_id, page_number)
                    if page_key in selected_keys:
                        raise ApiError(400, "A SEAI PDF page was selected more than once.")
                    selected_keys.add(page_key)
                    document_ids.add(document_id)
                    normalized_selections.append(
                        {
                            "document_id": document_id,
                            "page": page_number,
                            "annotations": selection.get("annotations"),
                        }
                    )
                with connect_db() as db:
                    _require_pdf_editable(db, house_id)
                    survey_note = None
                    if include_survey_note:
                        survey_row = db.execute(
                            "SELECT data FROM house_surveys WHERE house_id = ?",
                            (house_id,),
                        ).fetchone()
                        if survey_row is None:
                            raise ApiError(409, "Save a survey note before sharing selected PDF pages.")
                        try:
                            survey_data = json.loads(survey_row["data"])
                        except (json.JSONDecodeError, TypeError) as exc:
                            raise ApiError(500, "The saved house survey could not be read.") from exc
                        if not isinstance(survey_data, dict):
                            raise ApiError(500, "The saved house survey could not be read.")
                        survey_note = str(survey_data.get("notes") or "").strip()
                        if not survey_note:
                            raise ApiError(
                                409, "Save a survey note before sharing selected PDF pages."
                            )
                    rows = []
                    if document_ids:
                        application_ids = {
                            house_id,
                            _seai_application_ids_by_house(db, [house_id]).get(
                                house_id, ""
                            ),
                        } - {""}
                        application_placeholders = ",".join(
                            "?" for _ in application_ids
                        )
                        document_placeholders = ",".join(
                            "?" for _ in document_ids
                        )
                        rows = db.execute(
                            f"""
                            SELECT id, category, filename, content FROM seai_documents
                            WHERE application_id IN ({application_placeholders})
                                AND id IN ({document_placeholders})
                                AND lower(filename) LIKE '%.pdf' AND content IS NOT NULL
                            """,
                            (*sorted(application_ids), *sorted(document_ids)),
                        ).fetchall()
                selected_documents = {row["id"]: row for row in rows}
                if len(selected_documents) != len(document_ids):
                    raise ApiError(
                        404, "One or more selected SEAI PDFs were not found for this house."
                    )
                documents = dict(selected_documents)
                resolved_selections = []
                resolved_keys = set()
                for selection in normalized_selections:
                    if selection.get("blank_page"):
                        resolved_selections.append(selection)
                        continue
                    if selection.get("test_pdf_name"):
                        test_pdf_name = selection["test_pdf_name"]
                        test_pdf_path = (
                            STATIC_DIR
                            / "oldest-house-test"
                            / "synthetic"
                            / "pdfs"
                            / test_pdf_name
                        )
                        try:
                            content = test_pdf_path.read_bytes()
                        except OSError as exc:
                            raise ApiError(404, "Test PDF was not found.") from exc
                        annotations = selection.get("annotations")
                        if annotations is not None:
                            content = _annotate_pdf_document(
                                content,
                                annotations,
                                allow_ventilation_icons=True,
                                icon_images=icon_images,
                            )
                        if len(content) > SEAI_DOCUMENT_MAX_BYTES:
                            raise ApiError(
                                413,
                                "The selected test PDF exceeds the 20 MB document limit.",
                            )
                        resolved_selections.append(
                            {
                                "filename": test_pdf_name,
                                "page": selection["page"],
                                "pdf_content": content,
                            }
                        )
                        continue
                    selected_document = selected_documents[selection["document_id"]]
                    page_key = (
                        selected_document["id"],
                        selection["page"],
                    )
                    if page_key in resolved_keys:
                        raise ApiError(
                            400,
                            "A SEAI PDF page was selected more than once.",
                        )
                    resolved_keys.add(page_key)
                    content = selected_document["content"]
                    annotations = selection.get("annotations")
                    if annotations is not None:
                        content = _annotate_pdf_document(
                            content,
                            annotations,
                            allow_ventilation_icons=True,
                            icon_images=icon_images,
                        )
                    if len(content) > SEAI_DOCUMENT_MAX_BYTES:
                        raise ApiError(
                            413,
                            "The selected SEAI PDF exceeds the 20 MB document limit.",
                        )
                    resolved_selections.append(
                        {
                            "document_id": selected_document["id"],
                            "filename": selected_document["filename"],
                            "page": selection["page"],
                            "pdf_content": content,
                        }
                    )
                shared_pdf = _create_seai_document_share_pdf(
                    resolved_selections,
                    documents,
                    house_id,
                    survey_note,
                )
                suffix = "-with-survey-note" if include_survey_note else ""
                share_filename = f"{house_id}-selected-seai-pages{suffix}.pdf"
                self._send_pdf(share_filename, shared_pdf, attachment=True)
                return
            seai_vent_annotation_match = re.fullmatch(
                r"/api/seai-documents/(\d+)/vent-annotations", path
            )
            if seai_vent_annotation_match:
                with connect_db() as db:
                    if not _in_department(db, user, "Ventilation"):
                        raise ApiError(
                            403,
                            "Ventilation department access is required to annotate Surveyor PDFs.",
                        )
                    source = db.execute(
                        """
                        SELECT documents.id, documents.application_id, documents.filename,
                               documents.content
                        FROM seai_documents documents
                        JOIN selected_seai_documents selected
                          ON selected.document_id = documents.id
                         AND selected.house_id = documents.application_id
                        JOIN provided_houses houses
                          ON houses.id = selected.house_id
                        WHERE documents.id = ?
                          AND selected.created_by != ''
                          AND houses.plan_vent_required = 1
                          AND lower(documents.filename) LIKE '%.pdf'
                          AND documents.content IS NOT NULL
                        """,
                        (int(seai_vent_annotation_match.group(1)),),
                    ).fetchone()
                    if source is not None and not _ventilation_house_is_visible(
                        db, source["application_id"], user["id"]
                    ):
                        source = None
                if source is None:
                    raise ApiError(404, "Selected Surveyor PDF was not found.")
                payload = self._json_body()
                if not isinstance(payload, dict):
                    raise ApiError(400, "PDF annotations must be a JSON object.")
                annotations = payload.get("annotations")
                annotated_pdf = _annotate_pdf_document(
                    source["content"],
                    annotations,
                    allow_ventilation_icons=True,
                    icon_images=payload.get("icon_images"),
                )
                if len(annotated_pdf) > HOUSE_DOCUMENT_MAX_BYTES:
                    raise ApiError(413, "The annotated PDF exceeds the 8 MB Vent document limit.")
                annotated_stem = f"{Path(source['filename']).stem}-vent-annotated"
                annotated_filename = f"{annotated_stem}.pdf"
                shared_at = datetime.now().isoformat(timespec="seconds")
                shared_by = f"Vent annotated by {user['display_name']}"
                with connect_db() as db:
                    with db:
                        eligible = db.execute(
                            """
                            SELECT 1 FROM provided_houses
                            WHERE id = ? AND plan_vent_required = 1
                            """,
                            (source["application_id"],),
                        ).fetchone()
                        if eligible is None:
                            raise ApiError(
                                409, "Vent PDFs can only be annotated while Vent work is required."
                            )
                        document_count = db.execute(
                            "SELECT COUNT(*) FROM house_vent_documents WHERE house_id = ?",
                            (source["application_id"],),
                        ).fetchone()[0]
                        if document_count >= HOUSE_DOCUMENT_MAX_COUNT:
                            raise ApiError(
                                409, "This house already has the maximum of 25 Vent team PDFs."
                            )
                        suffix = 2
                        while db.execute(
                            """
                            SELECT 1 FROM house_vent_documents
                            WHERE house_id = ? AND filename = ?
                            """,
                            (source["application_id"], annotated_filename),
                        ).fetchone():
                            annotated_filename = f"{annotated_stem}-{suffix}.pdf"
                            suffix += 1
                        cursor = db.execute(
                            """
                            INSERT INTO house_vent_documents
                                (house_id, filename, content, size_bytes, shared_by, shared_at)
                            VALUES (?, ?, ?, ?, ?, ?)
                            """,
                            (
                                source["application_id"],
                                annotated_filename,
                                annotated_pdf,
                                len(annotated_pdf),
                                shared_by,
                                shared_at,
                            ),
                        )
                        db.execute(
                            """
                            INSERT INTO house_vent_document_annotations
                                (annotated_document_id, source_document_id,
                                 annotations_json, saved_at, source_seai_document_id)
                            VALUES (?, NULL, ?, ?, ?)
                            """,
                            (
                                cursor.lastrowid,
                                _pdf_annotation_metadata_json(annotations),
                                shared_at,
                                source["id"],
                            ),
                        )
                self._send_json(
                    201,
                    {
                        "id": cursor.lastrowid,
                        "house_id": source["application_id"],
                        "source_id": source["id"],
                        "filename": annotated_filename,
                        "size_bytes": len(annotated_pdf),
                        "shared_by": shared_by,
                        "shared_at": shared_at,
                        "updated": False,
                    },
                )
                return
            test_pdf_annotation_match = re.fullmatch(
                r"/api/survey-room-test-pdfs/([^/]+)/annotations", path
            )
            if test_pdf_annotation_match:
                if not (
                    _has_access_profile(user, "admin")
                    or _has_access_profile(user, "surveyor")
                ):
                    raise ApiError(
                        403,
                        "Administrator or surveyor access is required to annotate test PDFs.",
                    )
                pdf_name = test_pdf_annotation_match.group(1)
                if pdf_name not in SURVEY_ROOM_TEST_PDFS:
                    raise ApiError(404, "Test PDF was not found.")
                payload = self._json_body()
                if not isinstance(payload, dict):
                    raise ApiError(400, "PDF annotations must be a JSON object.")
                house_id = str(payload.get("house_id") or "").strip()
                if not house_id:
                    raise ApiError(400, "A house ID is required.")
                with connect_db() as db:
                    if not db.execute(
                        "SELECT 1 FROM provided_houses WHERE id = ?", (house_id,)
                    ).fetchone():
                        raise ApiError(404, "House was not found.")
                    _require_pdf_editable(db, house_id)
                raise ApiError(
                    409,
                    "Test PDF annotations are temporary. Add the page to Review selected pages and save the combined PDF.",
                )
            seai_annotation_match = re.fullmatch(
                r"/api/seai-documents/(\d+)/annotations", path
            )
            if seai_annotation_match:
                if not (_has_access_profile(user, "admin") or _has_access_profile(user, "surveyor")):
                    raise ApiError(
                        403, "Administrator or surveyor access is required to annotate SEAI PDFs."
                    )
                payload = self._json_body()
                if not isinstance(payload, dict):
                    raise ApiError(400, "PDF annotations must be a JSON object.")
                house_id = str(payload.get("house_id") or "").strip()
                if not house_id:
                    raise ApiError(400, "A house ID is required.")
                document_id = int(seai_annotation_match.group(1))
                with connect_db() as db:
                    columns = {
                        row["name"] for row in db.execute("PRAGMA table_info(seai_documents)")
                    }
                    if "content" not in columns:
                        raise ApiError(503, "SEAI PDF storage has not been initialized.")
                    source = db.execute(
                        """
                        SELECT id, application_id, category, filename, content
                        FROM seai_documents
                        WHERE id = ? AND lower(filename) LIKE '%.pdf'
                            AND content IS NOT NULL
                        """,
                        (document_id,),
                    ).fetchone()
                    if source is not None:
                        if not db.execute(
                            "SELECT 1 FROM provided_houses WHERE id = ?", (house_id,)
                        ).fetchone():
                            raise ApiError(404, "House was not found.")
                        application_ids = {
                            house_id,
                            _seai_application_ids_by_house(db, [house_id]).get(
                                house_id, ""
                            ),
                        }
                        if source["application_id"] not in application_ids:
                            raise ApiError(
                                404, "SEAI PDF was not found for this house."
                            )
                        _require_pdf_editable(db, house_id)
                if source is None:
                    raise ApiError(404, "SEAI PDF was not found.")
                raise ApiError(
                    409,
                    "SEAI PDF annotations are temporary. Add the page to Review selected pages and save the combined PDF.",
                )
            annotation_match = re.fullmatch(
                r"/api/house-documents/(\d+)/annotations", path
            )
            if annotation_match:
                if not (_has_access_profile(user, "admin") or _has_access_profile(user, "surveyor")):
                    raise ApiError(403, "Surveyor access is required to annotate house documents.")
                payload = self._json_body()
                if not isinstance(payload, dict):
                    raise ApiError(400, "PDF annotations must be a JSON object.")
                document_id = int(annotation_match.group(1))
                with connect_db() as db:
                    source = db.execute(
                        """
                        SELECT d.house_id, d.filename, d.content
                        FROM house_documents d
                        JOIN provided_houses p ON p.id = d.house_id
                        LEFT JOIN houses h ON h.id = p.id
                        WHERE d.id = ? AND p.ready_for_start = 1
                          AND (h.id IS NULL OR h.is_finished = 0)
                        """,
                        (document_id,),
                    ).fetchone()
                if source is None:
                    raise ApiError(404, "Document was not found.")
                annotated_pdf = _annotate_pdf_document(
                    source["content"],
                    payload.get("annotations"),
                )
                if len(annotated_pdf) > HOUSE_DOCUMENT_MAX_BYTES:
                    raise ApiError(413, "The annotated PDF exceeds the 8 MB document limit.")
                annotated_filename = (
                    f"{Path(source['filename']).stem}-annotated.pdf"
                )
                uploaded_at = datetime.now().isoformat(timespec="seconds")
                with connect_db() as db:
                    with db:
                        ready = db.execute(
                            """
                            SELECT 1 FROM provided_houses p LEFT JOIN houses h ON h.id = p.id
                            WHERE p.id = ? AND p.ready_for_start = 1
                              AND (h.id IS NULL OR h.is_finished = 0)
                            """,
                            (source["house_id"],),
                        ).fetchone()
                        if not ready:
                            raise ApiError(409, "PDFs can only be annotated for houses ready for start.")
                        document_count = db.execute(
                            "SELECT COUNT(*) FROM house_documents WHERE house_id = ?",
                            (source["house_id"],),
                        ).fetchone()[0]
                        if document_count >= HOUSE_DOCUMENT_MAX_COUNT:
                            raise ApiError(409, "This house already has the maximum of 25 PDF documents.")
                        cursor = db.execute(
                            """
                            INSERT INTO house_documents
                                (house_id, filename, content, size_bytes, uploaded_by, uploaded_at)
                            VALUES (?, ?, ?, ?, ?, ?)
                            """,
                            (
                                source["house_id"],
                                annotated_filename,
                                annotated_pdf,
                                len(annotated_pdf),
                                f"Annotated by {user['display_name']}",
                                uploaded_at,
                            ),
                        )
                self._send_json(
                    201,
                    {
                        "id": cursor.lastrowid,
                        "house_id": source["house_id"],
                        "filename": annotated_filename,
                        "size_bytes": len(annotated_pdf),
                        "uploaded_by": f"Annotated by {user['display_name']}",
                        "uploaded_at": uploaded_at,
                    },
                )
                return
            vent_annotation_match = re.fullmatch(
                r"/api/house-vent-documents/(\d+)/annotations", path
            )
            if vent_annotation_match:
                with connect_db() as db:
                    is_ventilation_user = _in_department(db, user, "Ventilation")
                if not is_ventilation_user:
                    raise ApiError(
                        403, "Ventilation department access is required to annotate shared PDFs."
                    )
                payload = self._json_body()
                if not isinstance(payload, dict):
                    raise ApiError(400, "PDF annotations must be a JSON object.")
                document_id = int(vent_annotation_match.group(1))
                with connect_db() as db:
                    source = db.execute(
                        """
                        SELECT documents.id, documents.house_id, documents.filename,
                               documents.content,
                               annotations.source_document_id AS annotation_source_id,
                               annotations.source_seai_document_id AS annotation_seai_source_id
                        FROM house_vent_documents documents
                        JOIN provided_houses houses ON houses.id = documents.house_id
                        LEFT JOIN house_vent_document_annotations annotations
                            ON annotations.annotated_document_id = documents.id
                        WHERE documents.id = ? AND houses.plan_vent_required = 1
                        """,
                        (document_id,),
                    ).fetchone()
                    if source is not None and source["annotation_source_id"] is not None:
                        editable_source = db.execute(
                            """
                            SELECT content FROM house_vent_documents
                            WHERE id = ? AND house_id = ?
                            """,
                            (source["annotation_source_id"], source["house_id"]),
                        ).fetchone()
                        if editable_source is None:
                            raise ApiError(
                                409, "The original Vent PDF is no longer available for editing."
                            )
                        source_content = editable_source["content"]
                        source_document_id = source["annotation_source_id"]
                        source_seai_document_id = None
                    elif source is not None and source["annotation_seai_source_id"] is not None:
                        editable_source = db.execute(
                            """
                            SELECT content FROM seai_documents
                            WHERE id = ? AND application_id = ?
                            """,
                            (source["annotation_seai_source_id"], source["house_id"]),
                        ).fetchone()
                        if editable_source is None:
                            raise ApiError(
                                409, "The original Surveyor PDF is no longer available for editing."
                            )
                        source_content = editable_source["content"]
                        source_document_id = None
                        source_seai_document_id = source["annotation_seai_source_id"]
                    elif source is not None:
                        source_content = source["content"]
                        source_document_id = document_id
                        source_seai_document_id = None
                if source is None:
                    raise ApiError(404, "Document was not found.")
                annotated_pdf = _annotate_pdf_document(
                    source_content,
                    payload.get("annotations"),
                    allow_ventilation_icons=True,
                    icon_images=payload.get("icon_images"),
                )
                if len(annotated_pdf) > HOUSE_DOCUMENT_MAX_BYTES:
                    raise ApiError(413, "The annotated PDF exceeds the 8 MB document limit.")
                edit_target_id = (
                    document_id
                    if (
                        source["annotation_source_id"] is not None
                        or source["annotation_seai_source_id"] is not None
                    )
                    else None
                )
                annotated_filename = source["filename"] if edit_target_id else (
                    f"{Path(source['filename']).stem}-vent-annotated.pdf"
                )
                shared_at = datetime.now().isoformat(timespec="seconds")
                shared_by = f"Vent annotated by {user['display_name']}"
                annotations_json = _pdf_annotation_metadata_json(
                    payload.get("annotations")
                )
                with connect_db() as db:
                    with db:
                        eligible = db.execute(
                            """
                            SELECT 1 FROM provided_houses
                            WHERE id = ? AND plan_vent_required = 1
                            """,
                            (source["house_id"],),
                        ).fetchone()
                        if eligible is None:
                            raise ApiError(
                                409, "Vent PDFs can only be annotated while Vent work is required."
                            )
                        if edit_target_id:
                            db.execute(
                                """
                                UPDATE house_vent_documents
                                SET content = ?, size_bytes = ?, shared_by = ?, shared_at = ?
                                WHERE id = ? AND house_id = ?
                                """,
                                (
                                    annotated_pdf,
                                    len(annotated_pdf),
                                    shared_by,
                                    shared_at,
                                    edit_target_id,
                                    source["house_id"],
                                ),
                            )
                            annotated_document_id = edit_target_id
                        else:
                            document_count = db.execute(
                                "SELECT COUNT(*) FROM house_vent_documents WHERE house_id = ?",
                                (source["house_id"],),
                            ).fetchone()[0]
                            if document_count >= HOUSE_DOCUMENT_MAX_COUNT:
                                raise ApiError(
                                    409, "This house already has the maximum of 25 Vent team PDFs."
                                )
                            suffix = 2
                            while db.execute(
                                """
                                SELECT 1 FROM house_vent_documents
                                WHERE house_id = ? AND filename = ?
                                """,
                                (source["house_id"], annotated_filename),
                            ).fetchone():
                                annotated_filename = (
                                    f"{Path(source['filename']).stem}-vent-annotated-{suffix}.pdf"
                                )
                                suffix += 1
                            cursor = db.execute(
                                """
                                INSERT INTO house_vent_documents
                                    (house_id, filename, content, size_bytes, shared_by, shared_at)
                                VALUES (?, ?, ?, ?, ?, ?)
                                """,
                                (
                                    source["house_id"],
                                    annotated_filename,
                                    annotated_pdf,
                                    len(annotated_pdf),
                                    shared_by,
                                    shared_at,
                                ),
                            )
                            annotated_document_id = cursor.lastrowid
                        db.execute(
                            """
                            INSERT OR REPLACE INTO house_vent_document_annotations
                                (annotated_document_id, source_document_id,
                                 annotations_json, saved_at, source_seai_document_id)
                            VALUES (?, ?, ?, ?, ?)
                            """,
                            (
                                annotated_document_id,
                                source_document_id,
                                annotations_json,
                                shared_at,
                                source_seai_document_id,
                            ),
                        )
                self._send_json(
                    200 if edit_target_id else 201,
                    {
                        "id": annotated_document_id,
                        "house_id": source["house_id"],
                        "source_id": source_document_id,
                        "filename": annotated_filename,
                        "size_bytes": len(annotated_pdf),
                        "shared_by": shared_by,
                        "shared_at": shared_at,
                        "updated": bool(edit_target_id),
                    },
                )
                return
            if path == "/api/house-survey":
                if not (_has_access_profile(user, "admin") or _has_access_profile(user, "surveyor")):
                    raise ApiError(403, "Surveyor access is required to record measurements.")
                payload = self._json_body()
                if not isinstance(payload, dict):
                    raise ApiError(400, "Survey data must be an object.")
                house_id = str(payload.get("house_id") or "").strip()
                survey = _validate_survey_payload(payload.get("survey"))
                with connect_db() as db:
                    with db:
                        ready = db.execute("""
                            SELECT p.plan_scaffolding_required,
                                   p.plan_esb_required,
                                   p.plan_plumber_required,
                                   p.plan_ceramic_fuse_board,
                                   p.plan_solar_pv_required,
                                   p.plan_pir_required,
                                   p.plan_order_variations,
                                   p.plan_windows_required
                            FROM provided_houses p LEFT JOIN houses h ON h.id = p.id
                            WHERE p.id = ? AND p.ready_for_start = 1
                              AND (h.id IS NULL OR h.is_finished = 0)
                        """, (house_id,)).fetchone()
                        if not ready:
                            raise ApiError(409, "Survey measurements can only be saved for houses ready for start.")
                        draft = db.execute(
                            "SELECT data FROM house_work_plan_drafts WHERE house_id = ?",
                            (house_id,),
                        ).fetchone()
                        draft_data = None
                        if draft:
                            try:
                                draft_data = json.loads(draft["data"])
                            except (TypeError, ValueError):
                                raise ApiError(
                                    500,
                                    "Saved pre-start work-plan draft is invalid.",
                                )
                            if not isinstance(draft_data, dict) or any(
                                not isinstance(draft_data.get(f"{trade}_required"), bool)
                                for trade in ("scaffolding", "esb", "plumber")
                            ):
                                raise ApiError(
                                    409,
                                    "Pre-start work plan is not saved. Choose and save Scaffolding, ESB, and Plumber requirements first.",
                                )
                            for trade in WORK_PLAN_TRADES:
                                required = draft_data.get(f"{trade}_required")
                                notes = draft_data.get(f"{trade}_notes", "")
                                if required is not None and not isinstance(required, bool):
                                    raise ApiError(
                                        500,
                                        "Saved pre-start work-plan draft is invalid.",
                                    )
                                if not isinstance(notes, str):
                                    raise ApiError(
                                        500,
                                        "Saved pre-start work-plan draft is invalid.",
                                    )
                                db.execute(
                                    f"""
                                    UPDATE provided_houses
                                    SET plan_{trade}_required = ?,
                                        plan_{trade}_notes = ?,
                                        plan_{trade}_done_by =
                                            CASE WHEN ? = 1 THEN plan_{trade}_done_by ELSE '' END,
                                        plan_{trade}_done_at =
                                            CASE WHEN ? = 1 THEN plan_{trade}_done_at ELSE '' END,
                                        plan_{trade}_done_by_user_id =
                                            CASE WHEN ? = 1 THEN plan_{trade}_done_by_user_id ELSE NULL END
                                    WHERE id = ?
                                    """,
                                    (
                                        int(required) if required is not None else None,
                                        notes,
                                        int(required) if required is not None else 0,
                                        int(required) if required is not None else 0,
                                        int(required) if required is not None else 0,
                                        house_id,
                                    ),
                                )
                            for question in WORK_PLAN_SURVEY_QUESTIONS:
                                answer = draft_data.get(question)
                                if answer is not None and not isinstance(answer, bool):
                                    raise ApiError(
                                        500,
                                        "Saved pre-start work-plan draft is invalid.",
                                    )
                                if answer is None:
                                    raise ApiError(
                                        409,
                                        "Answer every pre-start survey question before submitting the survey.",
                                    )
                            question_assignments = ", ".join(
                                f"plan_{question} = ?, "
                                f"plan_{question}_done_by = CASE "
                                f"WHEN plan_{question} IS ? THEN plan_{question}_done_by "
                                "ELSE '' END, "
                                f"plan_{question}_done_at = CASE "
                                f"WHEN plan_{question} IS ? THEN plan_{question}_done_at "
                                "ELSE '' END, "
                                f"plan_{question}_done_by_user_id = CASE "
                                f"WHEN plan_{question} IS ? THEN plan_{question}_done_by_user_id "
                                "ELSE NULL END"
                                for question in WORK_PLAN_SURVEY_QUESTIONS
                            )
                            question_values = []
                            for question in WORK_PLAN_SURVEY_QUESTIONS:
                                answer = (
                                    int(draft_data[question])
                                    if isinstance(draft_data.get(question), bool)
                                    else None
                                )
                                question_values.extend((answer, answer, answer, answer))
                            db.execute(
                                f"UPDATE provided_houses SET {question_assignments} WHERE id = ?",
                                (*question_values, house_id),
                            )
                        elif any(
                            ready[f"plan_{trade}_required"] is None
                            for trade in ("scaffolding", "esb", "plumber")
                        ):
                            raise ApiError(
                                409,
                                "Pre-start work plan is not saved. Save it before recording survey measurements.",
                            )
                        elif any(
                            ready[f"plan_{question}"] is None
                            for question in WORK_PLAN_SURVEY_QUESTIONS
                        ):
                            raise ApiError(
                                409,
                                "Answer every pre-start survey question before submitting the survey.",
                            )
                        db.execute(
                            """
                            DELETE FROM house_vent_assignments
                            WHERE house_id = ?
                              AND NOT EXISTS (
                                  SELECT 1 FROM provided_houses
                                  WHERE id = ? AND plan_vent_required = 1
                              )
                            """,
                            (house_id, house_id),
                        )
                        db.execute(
                            """
                            DELETE FROM house_role_assignments
                            WHERE house_id = ? AND role = 'supervisor'
                              AND EXISTS (
                                  SELECT 1 FROM app_users assigned_user
                                  WHERE assigned_user.id = house_role_assignments.user_id
                                    AND (
                                        assigned_user.supervisor_specialty = 'Plumbing' COLLATE NOCASE
                                        OR EXISTS (
                                            SELECT 1 FROM user_departments memberships
                                            JOIN departments
                                              ON departments.id = memberships.department_id
                                            WHERE memberships.user_id = assigned_user.id
                                              AND departments.name = 'Plumbing' COLLATE NOCASE
                                              AND departments.is_active = 1
                                        )
                                    )
                              )
                              AND NOT EXISTS (
                                  SELECT 1 FROM provided_houses
                                  WHERE id = ? AND plan_plumber_required = 1
                              )
                            """,
                            (house_id, house_id),
                        )
                        db.execute(
                            """
                            DELETE FROM house_plumbing_assignments
                            WHERE house_id = ?
                              AND NOT EXISTS (
                                  SELECT 1 FROM provided_houses
                                  WHERE id = ? AND plan_plumber_required = 1
                              )
                            """,
                            (house_id, house_id),
                        )
                        if survey.get("perimeter_m") is None:
                            raise ApiError(
                                400,
                                "Enter a surveyed perimeter greater than 0 and no more than 10,000 metres.",
                            )
                        updated_at = datetime.now().isoformat(timespec="seconds")
                        _record_house_survey_revision(
                            db, house_id, survey, user, updated_at
                        )
                        db.execute("""
                            INSERT INTO house_surveys(house_id, data, notes, updated_by, updated_at)
                            VALUES (?, ?, ?, ?, ?)
                            ON CONFLICT(house_id) DO UPDATE SET data = excluded.data,
                                notes = excluded.notes, updated_by = excluded.updated_by,
                                updated_at = excluded.updated_at
                        """, (house_id, json.dumps(survey, ensure_ascii=False), survey["notes"],
                                  user["display_name"], updated_at))
                        if draft_data is not None:
                            db.execute(
                                "DELETE FROM house_work_plan_drafts WHERE house_id = ?",
                                (house_id,),
                            )
                self._send_json(
                    200,
                    {
                        "house_id": house_id,
                        "saved": True,
                        "survey": survey,
                        "survey_completed": _survey_has_valid_perimeter(survey),
                        "updated_at": updated_at,
                        "work_plan_published": draft_data is not None,
                    },
                )
                return
            if path == "/api/house-notes":
                if not (_has_access_profile(user, "admin") or _has_access_profile(user, "surveyor")):
                    raise ApiError(
                        403,
                        "Administrator or surveyor access is required to edit house notes.",
                    )
                payload = self._json_body()
                if not isinstance(payload, dict):
                    raise ApiError(400, "House notes must be a JSON object.")
                house_id = str(payload.get("house_id") or "").strip()
                notes = str(payload.get("notes") or "").strip()
                if not house_id:
                    raise ApiError(400, "A house ID is required.")
                if len(notes) > 2000:
                    raise ApiError(400, "Notes must be 2,000 characters or fewer.")
                with connect_db() as db:
                    with db:
                        ready = db.execute(
                            """
                            SELECT 1 FROM provided_houses p LEFT JOIN houses h ON h.id = p.id
                            WHERE p.id = ? AND p.ready_for_start = 1
                              AND (h.id IS NULL OR h.is_finished = 0)
                            """,
                            (house_id,),
                        ).fetchone()
                        if not ready:
                            raise ApiError(409, "House notes can only be edited for houses ready for start.")
                        db.execute(
                            "UPDATE provided_houses SET notes = ? WHERE id = ?",
                            (notes, house_id),
                        )
                self._send_json(200, {"house_id": house_id, "notes": notes, "saved": True})
                return
            if path == "/api/survey-notes":
                if not (_has_access_profile(user, "admin") or _has_access_profile(user, "surveyor")):
                    raise ApiError(403, "Surveyor access is required to edit survey notes.")
                payload = self._json_body()
                if not isinstance(payload, dict):
                    raise ApiError(400, "Survey note data must be a JSON object.")
                house_id = str(payload.get("house_id") or "").strip()
                note = str(payload.get("notes") or "").strip()
                if not house_id:
                    raise ApiError(400, "A house ID is required.")
                if len(note) > 2000:
                    raise ApiError(400, "Survey notes must be 2,000 characters or fewer.")
                now = datetime.now().isoformat(timespec="seconds")
                with connect_db() as db:
                    with db:
                        ready = db.execute(
                            """
                            SELECT 1 FROM provided_houses p LEFT JOIN houses h ON h.id = p.id
                            WHERE p.id = ? AND p.ready_for_start = 1
                              AND (h.id IS NULL OR h.is_finished = 0)
                            """,
                            (house_id,),
                        ).fetchone()
                        if not ready:
                            raise ApiError(409, "Survey notes can only be edited for houses ready for start.")
                        existing = db.execute(
                            "SELECT data FROM house_surveys WHERE house_id = ?",
                            (house_id,),
                        ).fetchone()
                        survey = json.loads(existing["data"]) if existing else {
                            "windows": [],
                            "doors": [],
                            "house_corners": [],
                        }
                        survey["notes"] = note
                        _record_house_survey_revision(
                            db, house_id, survey, user, now
                        )
                        db.execute(
                            """
                            INSERT INTO house_surveys(house_id, data, notes, updated_by, updated_at)
                            VALUES (?, ?, ?, ?, ?)
                            ON CONFLICT(house_id) DO UPDATE SET
                                data = excluded.data,
                                notes = excluded.notes,
                                updated_by = excluded.updated_by,
                                updated_at = excluded.updated_at
                            """,
                            (
                                house_id,
                                json.dumps(survey, ensure_ascii=False),
                                note,
                                user["display_name"],
                                now,
                            ),
                        )
                self._send_json(
                    200,
                    {
                        "house_id": house_id,
                        "notes": note,
                        "survey": survey,
                        "survey_completed": _survey_has_valid_perimeter(survey),
                        "updated_by": user["display_name"],
                        "updated_at": now,
                        "saved": True,
                    },
                )
                return
            if path == "/api/supervisor/shared-seai-documents":
                if not (
                    _has_access_profile(user, "admin")
                    or _has_access_profile(user, "surveyor")
                ):
                    raise ApiError(
                        403,
                        "Administrator or surveyor access is required to send PDFs to the External Insulation team.",
                    )
                query = parse_qs(urlparse(self.path).query)
                house_id = query.get("house_id", [""])[0].strip()
                filename = Path(query.get("filename", [""])[0]).name.strip()
                if not house_id:
                    raise ApiError(400, "A house ID is required.")
                if not filename or not filename.lower().endswith(".pdf"):
                    raise ApiError(400, "External Insulation team documents must be PDF files.")
                file_data = self._body()
                if not file_data.startswith(b"%PDF-"):
                    raise ApiError(400, "The shared file is not a valid PDF.")
                if len(file_data) > HOUSE_DOCUMENT_MAX_BYTES:
                    raise ApiError(413, "The shared PDF must be smaller than 8 MB.")
                with connect_db() as db:
                    with db:
                        ready = db.execute(
                            """
                            SELECT 1 FROM provided_houses p LEFT JOIN houses h ON h.id = p.id
                            WHERE p.id = ? AND p.ready_for_start = 1
                              AND (h.id IS NULL OR h.is_finished = 0)
                            """,
                            (house_id,),
                        ).fetchone()
                        if not ready:
                            raise ApiError(404, "Ready house was not found.")
                        recipients = _external_insulation_supervisors(db)
                        if not recipients:
                            raise ApiError(404, "No active External Insulation supervisor accounts were found.")
                        delivery_ids = []
                        shared_at = datetime.now().isoformat(timespec="seconds")
                        for recipient in recipients:
                            count = db.execute(
                                """
                                SELECT COUNT(*) FROM seai_shared_pdf_deliveries
                                WHERE house_id = ? AND recipient_user_id = ?
                                """,
                                (house_id, recipient["id"]),
                            ).fetchone()[0]
                            if count >= HOUSE_DOCUMENT_MAX_COUNT:
                                raise ApiError(
                                    409,
                                    "This house already has the maximum of 25 External Insulation team PDFs.",
                                )
                            cursor = db.execute(
                                """
                                INSERT INTO seai_shared_pdf_deliveries
                                    (house_id, recipient_user_id, filename, content,
                                     size_bytes, shared_by, shared_at)
                                VALUES (?, ?, ?, ?, ?, ?, ?)
                                """,
                                (
                                    house_id,
                                    recipient["id"],
                                    filename,
                                    file_data,
                                    len(file_data),
                                    user["display_name"],
                                    shared_at,
                                ),
                            )
                            delivery_ids.append(cursor.lastrowid)
                self._send_json(
                    201,
                    {
                        "recipient_count": len(delivery_ids),
                        "house_id": house_id,
                        "filename": filename,
                    },
                )
                return
            if path == "/api/house-vent-documents":
                if not (_has_access_profile(user, "admin") or _has_access_profile(user, "surveyor")):
                    raise ApiError(403, "Surveyor access is required to share PDFs with the Vent team.")
                query = parse_qs(urlparse(self.path).query)
                house_id = query.get("house_id", [""])[0].strip()
                filename = Path(query.get("filename", [""])[0]).name.strip()
                if not house_id:
                    raise ApiError(400, "A house ID is required.")
                if not filename or not filename.lower().endswith(".pdf"):
                    raise ApiError(400, "Vent team documents must be PDF files.")
                file_data = self._body()
                if not file_data.startswith(b"%PDF-"):
                    raise ApiError(400, "The shared file is not a valid PDF.")
                with connect_db() as db:
                    with db:
                        ready = db.execute(
                            """
                            SELECT 1 FROM provided_houses p LEFT JOIN houses h ON h.id = p.id
                            WHERE p.id = ? AND p.ready_for_start = 1
                              AND (h.id IS NULL OR h.is_finished = 0)
                            """,
                            (house_id,),
                        ).fetchone()
                        if not ready:
                            raise ApiError(404, "Ready house was not found.")
                        count = db.execute(
                            "SELECT COUNT(*) FROM house_vent_documents WHERE house_id = ?",
                            (house_id,),
                        ).fetchone()[0]
                        if count >= HOUSE_DOCUMENT_MAX_COUNT:
                            raise ApiError(409, "This house already has the maximum of 25 Vent team PDFs.")
                        cursor = db.execute(
                            """
                            INSERT INTO house_vent_documents
                                (house_id, filename, content, size_bytes, shared_by, shared_at)
                            VALUES (?, ?, ?, ?, ?, ?)
                            """,
                            (
                                house_id,
                                filename,
                                file_data,
                                len(file_data),
                                user["display_name"],
                                datetime.now().isoformat(timespec="seconds"),
                            ),
                        )
                        db.execute(
                            """
                            UPDATE provided_houses
                            SET plan_vent_required = 1
                            WHERE id = ?
                            """,
                            (house_id,),
                        )
                self._send_json(
                    201,
                    {"id": cursor.lastrowid, "house_id": house_id, "filename": filename},
                )
                return
            if path == "/api/house-plumbing-photos":
                with connect_db() as db:
                    if not _is_plumbing_supervisor(db, user):
                        raise ApiError(
                            403,
                            "Only Plumbing Supervisors can upload Plumbing team photos.",
                        )
                content_type = self.headers.get("Content-Type", "")
                if not content_type.startswith("multipart/form-data;"):
                    raise ApiError(400, "Upload an image using the file picker.")
                body = self._body()
                match = re.search(r'boundary="?([^";]+)"?', content_type)
                if not match:
                    raise ApiError(400, "Invalid image upload.")
                try:
                    boundary = match.group(1).encode("ascii")
                except UnicodeEncodeError as exc:
                    raise ApiError(400, "Invalid image upload boundary.") from exc
                house_id = ""
                filename = ""
                activity = ""
                file_data = None
                for part in body.split(b"--" + boundary):
                    if b"\r\n\r\n" not in part:
                        continue
                    headers, content = part.split(b"\r\n\r\n", 1)
                    disposition = re.search(
                        rb'Content-Disposition:\s*form-data;\s*name="([^"]+)"(?:;\s*filename="([^"]*)")?',
                        headers,
                        re.IGNORECASE,
                    )
                    if not disposition:
                        continue
                    raw_content = content.rsplit(b"\r\n", 1)[0]
                    if disposition.group(1) == b"house_id":
                        try:
                            house_id = raw_content.decode("utf-8").strip()
                        except UnicodeDecodeError as exc:
                            raise ApiError(400, "Invalid house ID.") from exc
                    elif disposition.group(1) == b"activity":
                        try:
                            activity = raw_content.decode("utf-8").strip()
                        except UnicodeDecodeError as exc:
                            raise ApiError(400, "Invalid photo activity.") from exc
                    elif disposition.group(1) == b"file":
                        if file_data is not None:
                            raise ApiError(400, "Upload one image at a time.")
                        try:
                            filename = (
                                (disposition.group(2) or b"")
                                .decode("utf-8")
                                .replace("\\", "/")
                                .rsplit("/", 1)[-1]
                                .strip()
                            )
                        except UnicodeDecodeError as exc:
                            raise ApiError(400, "Invalid image filename.") from exc
                        file_data = raw_content
                if not house_id:
                    raise ApiError(400, "A house ID is required.")
                activity = activity or "Plumbing team photo"
                if len(activity) > 240:
                    raise ApiError(400, "Photo activity must be 240 characters or fewer.")
                if not filename or len(filename) > 200:
                    raise ApiError(400, "Choose a valid image filename.")
                if not file_data:
                    raise ApiError(400, "Choose an image to upload.")
                if len(file_data) > HOUSE_PLUMBING_PHOTO_MAX_BYTES:
                    raise ApiError(400, "House photos must be 8 MB or smaller.")
                suffix = Path(filename).suffix.lower()
                extension_formats = {
                    ".jpg": "JPEG",
                    ".jpeg": "JPEG",
                    ".png": "PNG",
                    ".webp": "WEBP",
                    ".gif": "GIF",
                    ".bmp": "BMP",
                    ".tif": "TIFF",
                    ".tiff": "TIFF",
                }
                expected_format = extension_formats.get(suffix)
                try:
                    if not expected_format:
                        raise ValueError("Unsupported image extension.")
                    with Image.open(BytesIO(file_data)) as image:
                        image_format = image.format
                        width, height = image.size
                        if (
                            image_format != expected_format
                            or width < 1
                            or height < 1
                            or width > 10000
                            or height > 10000
                            or width * height > 40_000_000
                        ):
                            raise ValueError("Unsupported image format or dimensions.")
                        image.verify()
                except (
                    OSError,
                    SyntaxError,
                    ValueError,
                    Image.DecompressionBombError,
                ) as exc:
                    raise ApiError(
                        400,
                        "House photos must be valid JPG, PNG, WebP, GIF, BMP, or TIFF images with a matching extension and no larger than 40 megapixels.",
                    ) from exc
                if image_format == "TIFF":
                    try:
                        with Image.open(BytesIO(file_data)) as image:
                            image.seek(0)
                            image = ImageOps.exif_transpose(image)
                            has_alpha = (
                                "A" in image.getbands()
                                or "transparency" in image.info
                            )
                            output_format = "PNG" if has_alpha else "JPEG"
                            output = BytesIO()
                            save_options = (
                                {"quality": 90, "optimize": True}
                                if output_format == "JPEG"
                                else {}
                            )
                            image.convert(
                                "RGBA" if has_alpha else "RGB"
                            ).save(
                                output,
                                format=output_format,
                                **save_options,
                            )
                    except (
                        OSError,
                        SyntaxError,
                        ValueError,
                        Image.DecompressionBombError,
                    ) as exc:
                        raise ApiError(
                            400,
                            "The selected TIFF photo could not be converted for viewing.",
                        ) from exc
                    file_data = output.getvalue()
                    if len(file_data) > HOUSE_PLUMBING_PHOTO_MAX_BYTES:
                        raise ApiError(
                            400,
                            "The converted photo exceeds the 8 MB upload limit.",
                        )
                    filename = str(
                        Path(filename).with_suffix(
                            ".png" if output_format == "PNG" else ".jpg"
                        )
                    )
                    mime_type = Image.MIME[output_format]
                else:
                    mime_type = {
                        "JPEG": "image/jpeg",
                        "PNG": "image/png",
                        "WEBP": "image/webp",
                        "GIF": "image/gif",
                        "BMP": "image/bmp",
                    }[image_format]
                with connect_db() as db:
                    with db:
                        db.execute("BEGIN IMMEDIATE")
                        assigned = db.execute(
                            """
                            SELECT 1
                            FROM provided_houses provided
                            WHERE provided.id = ?
                              AND provided.plan_plumber_required = 1
                              AND (
                                  EXISTS (
                                      SELECT 1 FROM house_plumbing_assignments assignment
                                      WHERE assignment.house_id = provided.id
                                        AND assignment.supervisor_user_id = ?
                                  )
                                  OR EXISTS (
                                      SELECT 1 FROM house_role_assignments assignment
                                      WHERE assignment.house_id = provided.id
                                        AND assignment.role = 'supervisor'
                                        AND assignment.user_id = ?
                                  )
                              )
                            """,
                            (house_id, user["id"], user["id"]),
                        ).fetchone()
                        if assigned is None:
                            raise ApiError(
                                404,
                                "That Plumbing house is not assigned to you.",
                            )
                        count = db.execute(
                            "SELECT COUNT(*) FROM house_plumbing_photos WHERE house_id = ?",
                            (house_id,),
                        ).fetchone()[0]
                        if count >= HOUSE_PLUMBING_PHOTO_MAX_COUNT:
                            raise ApiError(
                                409,
                                "This house already has the maximum of 25 Plumbing team photos.",
                            )
                        uploaded_at = datetime.now().isoformat(timespec="seconds")
                        cursor = db.execute(
                            """
                            INSERT INTO house_plumbing_photos
                                (house_id, filename, mime_type, content, size_bytes,
                                 uploaded_by, uploaded_at, activity)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                house_id,
                                filename,
                                mime_type,
                                file_data,
                                len(file_data),
                                user["display_name"],
                                uploaded_at,
                                activity,
                            ),
                        )
                self._send_json(
                    201,
                    {
                        "id": cursor.lastrowid,
                        "house_id": house_id,
                        "filename": filename,
                        "size_bytes": len(file_data),
                        "uploaded_by": user["display_name"],
                        "uploaded_at": uploaded_at,
                        "activity": activity,
                    },
                )
                return
            if path == "/api/house-vent-photos":
                if not _is_vent_photo_uploader(user):
                    raise ApiError(
                        403,
                        "Only the assigned Vent team account can upload house photos.",
                    )
                content_type = self.headers.get("Content-Type", "")
                if not content_type.startswith("multipart/form-data;"):
                    raise ApiError(400, "Upload an image using the file picker.")
                body = self._body()
                match = re.search(r'boundary="?([^";]+)"?', content_type)
                if not match:
                    raise ApiError(400, "Invalid image upload.")
                try:
                    boundary = match.group(1).encode("ascii")
                except UnicodeEncodeError as exc:
                    raise ApiError(400, "Invalid image upload boundary.") from exc
                house_id = ""
                filename = ""
                file_data = None
                for part in body.split(b"--" + boundary):
                    if b"\r\n\r\n" not in part:
                        continue
                    headers, content = part.split(b"\r\n\r\n", 1)
                    disposition = re.search(
                        rb'Content-Disposition:\s*form-data;\s*name="([^"]+)"(?:;\s*filename="([^"]*)")?',
                        headers,
                        re.IGNORECASE,
                    )
                    if not disposition:
                        continue
                    raw_content = content.rsplit(b"\r\n", 1)[0]
                    if disposition.group(1) == b"house_id":
                        try:
                            house_id = raw_content.decode("utf-8").strip()
                        except UnicodeDecodeError as exc:
                            raise ApiError(400, "Invalid house ID.") from exc
                    elif disposition.group(1) == b"file":
                        if file_data is not None:
                            raise ApiError(400, "Upload one image at a time.")
                        raw_filename = disposition.group(2) or b""
                        try:
                            filename = raw_filename.decode("utf-8").replace("\\", "/").rsplit("/", 1)[-1].strip()
                        except UnicodeDecodeError as exc:
                            raise ApiError(400, "Invalid image filename.") from exc
                        file_data = raw_content
                if not house_id:
                    raise ApiError(400, "A house ID is required.")
                if not filename or len(filename) > 200:
                    raise ApiError(400, "Choose a valid image filename.")
                if not file_data:
                    raise ApiError(400, "Choose an image to upload.")
                if len(file_data) > HOUSE_VENT_PHOTO_MAX_BYTES:
                    raise ApiError(400, "House photos must be 8 MB or smaller.")
                suffix = Path(filename).suffix.lower()
                extension_format = Image.registered_extensions().get(suffix)
                try:
                    with Image.open(BytesIO(file_data)) as image:
                        image_format = image.format
                        width, height = image.size
                        mime_type = Image.MIME.get(image_format, "")
                        if (
                            not extension_format
                            or image_format != extension_format
                            or not mime_type.startswith("image/")
                            or width < 1
                            or height < 1
                            or width > 10000
                            or height > 10000
                            or width * height > 40_000_000
                        ):
                            raise ValueError("Unsupported image format or dimensions.")
                        image.verify()
                except (
                    OSError,
                    SyntaxError,
                    ValueError,
                    Image.DecompressionBombError,
                ) as exc:
                    raise ApiError(
                        400,
                        "House photos must be valid image files with a matching extension and no larger than 40 megapixels.",
                    ) from exc
                if image_format not in {"JPEG", "PNG", "GIF", "WEBP", "BMP"}:
                    try:
                        with Image.open(BytesIO(file_data)) as image:
                            image.seek(0)
                            image = ImageOps.exif_transpose(image)
                            has_alpha = (
                                "A" in image.getbands()
                                or "transparency" in image.info
                            )
                            output_format = "PNG" if has_alpha else "JPEG"
                            output = BytesIO()
                            save_options = (
                                {"quality": 90, "optimize": True}
                                if output_format == "JPEG"
                                else {}
                            )
                            image.convert(
                                "RGBA" if has_alpha else "RGB"
                            ).save(
                                output,
                                format=output_format,
                                **save_options,
                            )
                    except (
                        OSError,
                        SyntaxError,
                        ValueError,
                        Image.DecompressionBombError,
                    ) as exc:
                        raise ApiError(
                            400,
                            "The selected photo could not be converted for viewing.",
                        ) from exc
                    file_data = output.getvalue()
                    if len(file_data) > HOUSE_VENT_PHOTO_MAX_BYTES:
                        raise ApiError(
                            400,
                            "The converted house photo exceeds the 8 MB limit.",
                        )
                    filename = str(
                        Path(filename).with_suffix(
                            ".png" if output_format == "PNG" else ".jpg"
                        )
                    )
                    mime_type = Image.MIME[output_format]
                with connect_db() as db:
                    with db:
                        if not _ventilation_house_is_visible(
                            db, house_id, user["id"]
                        ):
                            raise ApiError(404, "Assigned Vent house was not found.")
                        count = db.execute(
                            "SELECT COUNT(*) FROM house_vent_photos WHERE house_id = ?",
                            (house_id,),
                        ).fetchone()[0]
                        if count >= HOUSE_VENT_PHOTO_MAX_COUNT:
                            raise ApiError(409, "This house already has the maximum of 25 Vent team photos.")
                        cursor = db.execute(
                            """
                            INSERT INTO house_vent_photos
                                (house_id, filename, mime_type, content, size_bytes,
                                 uploaded_by, uploaded_at)
                            VALUES (?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                house_id,
                                filename,
                                mime_type,
                                file_data,
                                len(file_data),
                                user["display_name"],
                                datetime.now().isoformat(timespec="seconds"),
                            ),
                        )
                self._send_json(
                    201,
                    {
                        "id": cursor.lastrowid,
                        "house_id": house_id,
                        "filename": filename,
                        "size_bytes": len(file_data),
                    },
                )
                return
            if path == "/api/house-documents":
                if not (_has_access_profile(user, "admin") or _has_access_profile(user, "surveyor")):
                    raise ApiError(403, "Surveyor access is required to upload house documents.")
                content_type = self.headers.get("Content-Type", "")
                if not content_type.startswith("multipart/form-data;"):
                    raise ApiError(400, "Upload a PDF using the file picker.")
                body = self._body()
                match = re.search(r'boundary="?([^";]+)"?', content_type)
                if not match:
                    raise ApiError(400, "Invalid PDF upload.")
                try:
                    boundary = match.group(1).encode("ascii")
                except UnicodeEncodeError as exc:
                    raise ApiError(400, "Invalid PDF upload boundary.") from exc
                house_id = ""
                filename = ""
                file_data = None
                for part in body.split(b"--" + boundary):
                    if b"\r\n\r\n" not in part:
                        continue
                    headers, content = part.split(b"\r\n\r\n", 1)
                    disposition = re.search(
                        rb'Content-Disposition:\s*form-data;\s*name="([^"]+)"(?:;\s*filename="([^"]*)")?',
                        headers,
                        re.IGNORECASE,
                    )
                    if not disposition:
                        continue
                    raw_content = content.rsplit(b"\r\n", 1)[0]
                    if disposition.group(1) == b"house_id":
                        try:
                            house_id = raw_content.decode("utf-8").strip()
                        except UnicodeDecodeError as exc:
                            raise ApiError(400, "Invalid house ID.") from exc
                    elif disposition.group(1) == b"file":
                        raw_filename = disposition.group(2) or b""
                        try:
                            filename = Path(raw_filename.decode("utf-8")).name.strip()
                        except (UnicodeDecodeError, ValueError) as exc:
                            raise ApiError(400, "Invalid PDF filename.") from exc
                        file_data = raw_content
                if not house_id:
                    raise ApiError(400, "A house ID is required.")
                if not filename or not filename.lower().endswith(".pdf"):
                    raise ApiError(400, "House documents must be PDF files.")
                if not file_data:
                    raise ApiError(400, "Choose a PDF file to upload.")
                if len(file_data) > HOUSE_DOCUMENT_MAX_BYTES:
                    raise ApiError(400, "PDF files must be 8 MB or smaller.")
                if not file_data.startswith(b"%PDF-"):
                    raise ApiError(400, "The selected file is not a valid PDF.")
                with connect_db() as db:
                    with db:
                        ready = db.execute(
                            """
                            SELECT 1 FROM provided_houses p LEFT JOIN houses h ON h.id = p.id
                            WHERE p.id = ? AND p.ready_for_start = 1
                              AND (h.id IS NULL OR h.is_finished = 0)
                            """,
                            (house_id,),
                        ).fetchone()
                        if not ready:
                            raise ApiError(404, "Ready house was not found.")
                        document_count = db.execute(
                            "SELECT COUNT(*) FROM house_documents WHERE house_id = ?",
                            (house_id,),
                        ).fetchone()[0]
                        if document_count >= HOUSE_DOCUMENT_MAX_COUNT:
                            raise ApiError(409, "This house already has the maximum of 25 PDF documents.")
                        cursor = db.execute(
                            """
                            INSERT INTO house_documents
                                (house_id, filename, content, size_bytes, uploaded_by, uploaded_at)
                            VALUES (?, ?, ?, ?, ?, ?)
                            """,
                            (
                                house_id,
                                filename,
                                file_data,
                                len(file_data),
                                user["display_name"],
                                datetime.now().isoformat(timespec="seconds"),
                            ),
                        )
                self._send_json(
                    201,
                    {
                        "id": cursor.lastrowid,
                        "house_id": house_id,
                        "filename": filename,
                        "size_bytes": len(file_data),
                    },
                )
                return
            if path == "/api/houses/plan":
                if not (_has_access_profile(user, "admin") or _has_access_profile(user, "assign")):
                    raise ApiError(
                        403,
                        "Administrator or Assign access is required to update house readiness.",
                    )
            elif path == "/api/supervisor/house-assignment":
                if not _has_access_profile(user, "supervisor"):
                    raise ApiError(
                        403,
                        "Supervisor access is required to assign a delivery person and team.",
                    )
            elif path == "/api/supervisor/ewi-assignment":
                if not _has_access_profile(user, "supervisor"):
                    raise ApiError(
                        403,
                        "Supervisor access is required to assign EWI work.",
                    )
            elif path == "/api/teamlead/ewi-work/complete":
                if not _has_access_profile(user, "teamlead"):
                    raise ApiError(
                        403,
                        "Teamlead access is required to complete assigned EWI work.",
                    )
            elif path == "/api/teamlead/insulation-work/complete":
                if not _has_access_profile(user, "teamlead"):
                    raise ApiError(
                        403,
                        "Teamlead access is required to complete assigned insulation work.",
                    )
            elif path == "/api/house-assignments":
                if not (_has_access_profile(user, "admin") or _has_access_profile(user, "assign")):
                    raise ApiError(
                        403,
                        "Administrator or Assign access is required to manage house assignments.",
                    )
            elif path == "/api/ventilation-coordinator/assignment":
                if not _has_access_profile(user, "ventilation_coordinator"):
                    raise ApiError(403, "Vent Coordinator access is required.")
            elif path == "/api/ventilation-coordinator/insulation-assignment":
                if not _has_access_profile(user, "ventilation_coordinator"):
                    raise ApiError(403, "Vent Coordinator access is required.")
            elif path == "/api/plumbing-coordinator/assignment":
                if not _has_access_profile(user, "plumbing_coordinator"):
                    raise ApiError(403, "Plumbing Coordinator access is required.")
            elif path == "/api/team-roster" or re.fullmatch(
                r"/api/team-roster/\d+/delete", path
            ):
                if not any(
                    _has_access_profile(user, profile)
                    for profile in ("admin", "supervisor", "teamlead")
                ):
                    raise ApiError(
                        403,
                        "Supervisor, Teamlead, or administrator access is required to manage team rosters.",
                    )
            elif path == "/api/houses" and _has_access_profile(user, "assign"):
                pass
            elif path == "/api/houses/return-ready" and (
                _has_access_profile(user, "admin")
                or _has_access_profile(user, "assign")
            ):
                pass
            elif path not in {
                "/api/movements",
                "/api/movements/batch",
                "/api/plumbing-status",
                "/api/house-checklist",
                "/api/house-work-plan",
                "/api/house-work-plan/complete",
                "/api/house-vent-photos",
                "/api/house-plumbing-photos",
                "/api/seai-measure-variation",
                "/api/teamlead/ewi-work/complete",
                "/api/teamlead/insulation-work/complete",
            }:
                if not self._admin_required(user):
                    return
            if path == "/api/ventilation-coordinator/insulation-assignment":
                payload = self._json_body()
                if not isinstance(payload, dict) or set(payload) != {
                    "house_id",
                    "work_type",
                    "teamlead_user_id",
                }:
                    raise ApiError(
                        400, "Choose a house, insulation work type, and Teamlead."
                    )
                house_id = payload["house_id"]
                if (
                    not isinstance(house_id, str)
                    or not house_id.strip()
                    or len(house_id.strip()) > 120
                ):
                    raise ApiError(400, "Enter a valid house ID.")
                house_id = house_id.strip()
                work_type = payload["work_type"]
                if work_type not in {"attic", "cavity"}:
                    raise ApiError(400, "Choose Attic or Cavity insulation work.")
                try:
                    teamlead_user_id = int(payload["teamlead_user_id"])
                except (TypeError, ValueError, OverflowError) as exc:
                    raise ApiError(400, "Choose an active Teamlead.") from exc
                if isinstance(payload["teamlead_user_id"], bool):
                    raise ApiError(400, "Choose an active Teamlead.")
                now = datetime.now().isoformat(timespec="seconds")
                with connect_db() as db:
                    with db:
                        if not _ventilation_coordinator_house_is_visible(db, house_id):
                            raise ApiError(
                                409,
                                "Choose a surveyed house that is Ready for Start.",
                            )
                        if work_type not in _attic_cavity_quantities(
                            db, [house_id]
                        ).get(house_id, {}):
                            raise ApiError(
                                409,
                                "That house does not currently require this insulation work.",
                            )
                        existing = db.execute(
                            """
                            SELECT completed_at FROM house_insulation_assignments
                            WHERE house_id = ? AND work_type = ?
                            """,
                            (house_id, work_type),
                        ).fetchone()
                        if existing and existing["completed_at"]:
                            raise ApiError(
                                409,
                                "Completed insulation work cannot be reassigned.",
                            )
                        teamlead = db.execute(
                            """
                            SELECT users.id, users.display_name, users.team_name
                            FROM app_users users
                            WHERE users.id = ? AND users.is_active = 1
                              AND lower(COALESCE(users.team_name, '')) LIKE ?
                              AND EXISTS (
                                  SELECT 1 FROM app_role_permissions access
                                  WHERE access.role_key = users.role
                                    AND access.permission_key = 'access.teamlead'
                                    AND access.is_allowed = 1
                              )
                            """,
                            (teamlead_user_id, f"%{work_type}%"),
                        ).fetchone()
                        if teamlead is None:
                            raise ApiError(
                                400,
                                f"Choose an active {work_type.title()} Teamlead.",
                            )
                        db.execute(
                            """
                            INSERT INTO house_insulation_assignments
                                (house_id, work_type, teamlead_user_id, assigned_by,
                                 assigned_at)
                            VALUES (?, ?, ?, ?, ?)
                            ON CONFLICT(house_id, work_type) DO UPDATE SET
                                teamlead_user_id = excluded.teamlead_user_id,
                                assigned_by = excluded.assigned_by,
                                assigned_at = excluded.assigned_at,
                                completed_by = '',
                                completed_at = ''
                            """,
                            (
                                house_id,
                                work_type,
                                teamlead_user_id,
                                user["display_name"],
                                now,
                            ),
                        )
                self._send_json(
                    200,
                    {
                        "house_id": house_id,
                        "work_type": work_type,
                        "teamlead_user_id": teamlead["id"],
                        "teamlead_name": teamlead["display_name"],
                        "team_name": teamlead["team_name"],
                        "assigned_by": user["display_name"],
                        "assigned_at": now,
                    },
                )
            elif path == "/api/ventilation-coordinator/assignment":
                payload = self._json_body()
                if not isinstance(payload, dict) or set(payload) != {
                    "house_id",
                    "teamlead_user_id",
                }:
                    raise ApiError(400, "Choose a house and Ventilation Teamlead.")
                if not isinstance(payload["house_id"], str):
                    raise ApiError(400, "Enter a valid house ID.")
                house_id = payload["house_id"].strip()
                if not house_id or len(house_id) > 120:
                    raise ApiError(400, "Enter a valid house ID.")
                teamlead_user_id = payload["teamlead_user_id"]
                if isinstance(teamlead_user_id, bool):
                    raise ApiError(400, "Choose an active Ventilation Teamlead.")
                try:
                    teamlead_user_id = int(teamlead_user_id)
                except (TypeError, ValueError, OverflowError) as exc:
                    raise ApiError(
                        400, "Choose an active Ventilation Teamlead."
                    ) from exc
                now = datetime.now().isoformat(timespec="seconds")
                with connect_db() as db:
                    with db:
                        house = db.execute(
                            """
                            SELECT p.id, h.is_finished
                            FROM provided_houses p
                            LEFT JOIN houses h ON h.id = p.id
                            WHERE p.id = ? AND p.ready_for_start = 1
                              AND p.plan_vent_required = 1
                            """,
                            (house_id,),
                        ).fetchone()
                        if (
                            house is None
                            or house["is_finished"] == 1
                            or not _ventilation_coordinator_house_is_visible(
                                db, house_id
                            )
                        ):
                            raise ApiError(
                                409,
                                "Choose an open house with required Vent work.",
                            )
                        teamlead = db.execute(
                            """
                            SELECT users.id, users.display_name, users.team_name
                            FROM app_users users
                            WHERE users.id = ? AND users.is_active = 1
                              AND EXISTS (
                                  SELECT 1 FROM app_role_permissions access
                                  WHERE access.role_key = users.role
                                    AND access.permission_key = 'access.teamlead'
                                    AND access.is_allowed = 1
                              )
                              AND EXISTS (
                                  SELECT 1 FROM user_departments memberships
                                  JOIN departments ON departments.id = memberships.department_id
                                  WHERE memberships.user_id = users.id
                                    AND departments.name = 'Ventilation' COLLATE NOCASE
                                    AND departments.is_active = 1
                              )
                            """,
                            (teamlead_user_id,),
                        ).fetchone()
                        if teamlead is None:
                            raise ApiError(
                                400, "Choose an active Ventilation Teamlead."
                            )
                        db.execute(
                            """
                            INSERT INTO house_vent_assignments
                                (house_id, teamlead_user_id, assigned_by, assigned_at)
                            VALUES (?, ?, ?, ?)
                            ON CONFLICT(house_id) DO UPDATE SET
                                teamlead_user_id = excluded.teamlead_user_id,
                                assigned_by = excluded.assigned_by,
                                assigned_at = excluded.assigned_at
                            """,
                            (
                                house_id,
                                teamlead_user_id,
                                user["display_name"],
                                now,
                            ),
                        )
                self._send_json(
                    200,
                    {
                        "house_id": house_id,
                        "teamlead_user_id": teamlead["id"],
                        "teamlead_name": teamlead["display_name"],
                        "team_name": teamlead["team_name"],
                        "assigned_at": now,
                    },
                )
            elif path == "/api/plumbing-coordinator/assignment":
                payload = self._json_body()
                if not isinstance(payload, dict) or set(payload) != {
                    "house_id",
                    "supervisor_user_id",
                }:
                    raise ApiError(400, "Choose a house and Plumbing Supervisor.")
                if not isinstance(payload["house_id"], str):
                    raise ApiError(400, "Enter a valid house ID.")
                house_id = payload["house_id"].strip()
                if not house_id or len(house_id) > 120:
                    raise ApiError(400, "Enter a valid house ID.")
                supervisor_user_id = payload["supervisor_user_id"]
                if isinstance(supervisor_user_id, bool):
                    raise ApiError(400, "Choose an active Plumbing Supervisor.")
                try:
                    supervisor_user_id = int(supervisor_user_id)
                except (TypeError, ValueError, OverflowError) as exc:
                    raise ApiError(
                        400, "Choose an active Plumbing Supervisor."
                    ) from exc
                now = datetime.now().isoformat(timespec="seconds")
                with connect_db() as db:
                    with db:
                        house = db.execute(
                            """
                            SELECT p.id, h.is_finished,
                                   COALESCE(hc.plumber_status, p.plan_plumber, 'not_done')
                                       AS plumber_status
                            FROM provided_houses p
                            LEFT JOIN houses h ON h.id = p.id
                            LEFT JOIN house_checklists hc ON hc.house_id = p.id
                            WHERE p.id = ? AND p.plan_plumber_required = 1
                            """,
                            (house_id,),
                        ).fetchone()
                        if (
                            house is None
                            or house["is_finished"] == 1
                            or house["plumber_status"] in {"done", "not_necessary"}
                        ):
                            raise ApiError(
                                409,
                                "Choose a house with outstanding plumbing work.",
                            )
                        supervisor = db.execute(
                            """
                            SELECT users.id, users.display_name
                            FROM app_users users
                            WHERE users.id = ? AND users.is_active = 1
                              AND EXISTS (
                                  SELECT 1 FROM app_role_permissions access
                                  WHERE access.role_key = users.role
                                    AND access.permission_key = 'access.supervisor'
                                    AND access.is_allowed = 1
                              )
                              AND (
                                  users.supervisor_specialty = 'Plumbing' COLLATE NOCASE
                                  OR EXISTS (
                                      SELECT 1 FROM user_departments memberships
                                      JOIN departments ON departments.id = memberships.department_id
                                      WHERE memberships.user_id = users.id
                                        AND departments.name = 'Plumbing' COLLATE NOCASE
                                        AND departments.is_active = 1
                                  )
                              )
                            """,
                            (supervisor_user_id,),
                        ).fetchone()
                        if supervisor is None:
                            raise ApiError(
                                400, "Choose an active Plumbing Supervisor."
                            )
                        db.execute(
                            """
                            INSERT INTO house_plumbing_assignments
                                (house_id, supervisor_user_id, assigned_by, assigned_at)
                            VALUES (?, ?, ?, ?)
                            ON CONFLICT(house_id) DO UPDATE SET
                                supervisor_user_id = excluded.supervisor_user_id,
                                assigned_by = excluded.assigned_by,
                                assigned_at = excluded.assigned_at
                            """,
                            (
                                house_id,
                                supervisor["id"],
                                user["display_name"],
                                now,
                            ),
                        )
                self._send_json(
                    200,
                    {
                        "house_id": house_id,
                        "supervisor_user_id": supervisor["id"],
                        "supervisor_name": supervisor["display_name"],
                        "assigned_at": now,
                    },
                )
            elif path == "/api/users":
                payload = self._json_body()
                if not isinstance(payload, dict):
                    raise ApiError(400, "Employee details must be a JSON object.")
                with connect_db() as db:
                    with db:
                        user_id = _insert_user(
                            db,
                            payload.get("username"),
                            payload.get("display_name"),
                            payload.get("pin"),
                            payload.get("role", "employee"),
                            payload.get("supervisor_specialty", ""),
                            payload.get("team_name", ""),
                        )
                        if "department_ids" in payload or "manager_user_id" in payload:
                            existing_departments = [
                                row["department_id"]
                                for row in db.execute(
                                    "SELECT department_id FROM user_departments "
                                    "WHERE user_id = ? ORDER BY is_primary DESC",
                                    (user_id,),
                                )
                            ]
                            _set_user_organization(
                                db,
                                user_id,
                                payload.get("department_ids", existing_departments),
                                payload.get("manager_user_id"),
                            )
                        created = db.execute(
                            """
                            SELECT id, username, display_name, role,
                                   supervisor_specialty, team_name
                            FROM app_users WHERE id = ?
                            """,
                            (user_id,),
                        ).fetchone()
                self._send_json(201, {"user": _public_user(created)})
            elif path == "/api/departments":
                payload = self._json_body()
                if not isinstance(payload, dict):
                    raise ApiError(400, "Department details must be a JSON object.")
                name = payload.get("name")
                if not isinstance(name, str):
                    raise ApiError(400, "Department name must be text.")
                name = name.strip()
                if not name or len(name) > 120:
                    raise ApiError(400, "Department name must be between 1 and 120 characters.")
                with connect_db() as db:
                    with db:
                        if db.execute(
                            "SELECT 1 FROM departments WHERE name = ? COLLATE NOCASE",
                            (name,),
                        ).fetchone():
                            raise ApiError(409, "A department with that name already exists.")
                        cursor = db.execute(
                            "INSERT INTO departments(name) VALUES (?)", (name,)
                        )
                        department = db.execute(
                            "SELECT id, name, is_active FROM departments WHERE id = ?",
                            (cursor.lastrowid,),
                        ).fetchone()
                self._send_json(201, {"department": dict(department)})
            elif path == "/api/roles":
                payload = self._json_body()
                if not isinstance(payload, dict):
                    raise ApiError(400, "Role details must be a JSON object.")
                role_key = payload.get("role_key")
                display_name = payload.get("display_name")
                if not isinstance(role_key, str) or not re.fullmatch(
                    r"[a-z][a-z0-9_]{1,48}", role_key
                ):
                    raise ApiError(400, "Role key must be 2-49 lowercase letters, numbers, or underscores.")
                if not isinstance(display_name, str):
                    raise ApiError(400, "Role name must be text.")
                display_name = display_name.strip()
                if not display_name or len(display_name) > 80:
                    raise ApiError(400, "Role name must be between 1 and 80 characters.")
                with connect_db() as db:
                    with db:
                        if db.execute(
                            "SELECT 1 FROM app_roles WHERE role_key = ? OR display_name = ? COLLATE NOCASE",
                            (role_key, display_name),
                        ).fetchone():
                            raise ApiError(409, "A role with that key or name already exists.")
                        db.execute(
                            "INSERT INTO app_roles(role_key, display_name) VALUES (?, ?)",
                            (role_key, display_name),
                        )
                        role = db.execute(
                            "SELECT role_key, display_name, is_active FROM app_roles WHERE role_key = ?",
                            (role_key,),
                        ).fetchone()
                self._send_json(201, {"role": dict(role)})
            elif role_permissions_match := re.fullmatch(
                r"/api/roles/([a-z][a-z0-9_]{1,48})/permissions", path
            ):
                payload = self._json_body()
                if not isinstance(payload, dict):
                    raise ApiError(400, "Permission details must be a JSON object.")
                permissions = payload.get("permissions")
                if not isinstance(permissions, list) or not all(
                    isinstance(permission, str) for permission in permissions
                ):
                    raise ApiError(400, "Choose a valid permission list.")
                permissions = list(dict.fromkeys(permissions))
                role_key = role_permissions_match.group(1)
                with connect_db() as db:
                    with db:
                        role = db.execute(
                            "SELECT role_key FROM app_roles WHERE role_key = ?",
                            (role_key,),
                        ).fetchone()
                        if role is None:
                            raise ApiError(404, "Role was not found.")
                        placeholders = ",".join("?" for _ in permissions) or "''"
                        valid = {
                            row["permission_key"]
                            for row in db.execute(
                                f"SELECT permission_key FROM app_permissions "
                                f"WHERE permission_key IN ({placeholders})",
                                permissions,
                            )
                        }
                        if valid != set(permissions):
                            raise ApiError(400, "Choose only permissions from the catalog.")
                        if "*" in permissions and role_key != "admin":
                            raise ApiError(
                                400, "Unrestricted access can only be assigned to the Admin role."
                            )
                        if role_key == "admin" and "*" not in permissions:
                            raise ApiError(
                                409, "The Admin role must retain unrestricted access."
                            )
                        db.execute(
                            "DELETE FROM app_role_permissions WHERE role_key = ?",
                            (role_key,),
                        )
                        db.executemany(
                            """
                            INSERT INTO app_role_permissions
                                (role_key, permission_key, is_allowed)
                            VALUES (?, ?, 1)
                            """,
                            ((role_key, permission) for permission in permissions),
                        )
                        has_admin = db.execute(
                            """
                            SELECT 1
                            FROM app_users users
                            JOIN app_role_permissions assignments
                              ON assignments.role_key = users.role
                            WHERE users.is_active = 1
                              AND assignments.is_allowed = 1
                              AND assignments.permission_key IN ('*', 'access.admin')
                            LIMIT 1
                            """
                        ).fetchone()
                        if has_admin is None:
                            raise ApiError(
                                409, "At least one active account must retain administrator access."
                            )
                self._send_json(200, {"ok": True})
            elif plumbing_photo_access_match := re.fullmatch(
                r"/api/users/(\d+)/plumbing-photo-access", path
            ):
                if not self._admin_required(user):
                    return
                payload = self._json_body()
                if (
                    not isinstance(payload, dict)
                    or set(payload) != {"can_view"}
                    or not isinstance(payload["can_view"], bool)
                ):
                    raise ApiError(
                        400, "Choose whether this account can view Plumbing photos."
                    )
                user_id = int(plumbing_photo_access_match.group(1))
                with connect_db() as db:
                    with db:
                        if not db.execute(
                            "SELECT 1 FROM app_users WHERE id = ?", (user_id,)
                        ).fetchone():
                            raise ApiError(404, "Account was not found.")
                        if payload["can_view"]:
                            db.execute(
                                """
                                INSERT INTO app_user_plumbing_photo_access
                                    (user_id, granted_by, granted_at)
                                VALUES (?, ?, ?)
                                ON CONFLICT(user_id) DO UPDATE SET
                                    granted_by = excluded.granted_by,
                                    granted_at = excluded.granted_at
                                """,
                                (
                                    user_id,
                                    user["display_name"],
                                    datetime.now().isoformat(timespec="seconds"),
                                ),
                            )
                        else:
                            db.execute(
                                "DELETE FROM app_user_plumbing_photo_access "
                                "WHERE user_id = ?",
                                (user_id,),
                            )
                self._send_json(200, {"ok": True, "can_view": payload["can_view"]})
            elif organization_match := re.fullmatch(
                r"/api/users/(\d+)/organization", path
            ):
                payload = self._json_body()
                if not isinstance(payload, dict):
                    raise ApiError(400, "Organization details must be a JSON object.")
                user_id = int(organization_match.group(1))
                with connect_db() as db:
                    with db:
                        if db.execute(
                            "SELECT 1 FROM app_users WHERE id = ?", (user_id,)
                        ).fetchone() is None:
                            raise ApiError(404, "Account was not found.")
                        _set_user_organization(
                            db,
                            user_id,
                            payload.get("department_ids"),
                            payload.get("manager_user_id"),
                        )
                self._send_json(200, {"ok": True})
            elif team_name_match := re.fullmatch(r"/api/users/(\d+)/team-name", path):
                payload = self._json_body()
                if not isinstance(payload, dict):
                    raise ApiError(400, "Team details must be a JSON object.")
                team_name = payload.get("team_name")
                if not isinstance(team_name, str):
                    raise ApiError(400, "Team name must be text.")
                team_name = team_name.strip()
                if len(team_name) > 120:
                    raise ApiError(400, "Team name must be 120 characters or fewer.")
                user_id = int(team_name_match.group(1))
                with connect_db() as db:
                    with db:
                        teamlead = db.execute(
                            """
                            SELECT id, username, display_name, role,
                                   supervisor_specialty, team_name
                            FROM app_users WHERE id = ?
                            """,
                            (user_id,),
                        ).fetchone()
                        if teamlead is None:
                            raise ApiError(404, "Account was not found.")
                        if not _role_has_access(
                            db, teamlead["role"], "teamlead"
                        ):
                            raise ApiError(
                                409, "Only Teamlead accounts can have a team name."
                            )
                        db.execute(
                            "UPDATE app_users SET team_name = ? WHERE id = ?",
                            (team_name, user_id),
                        )
                        teamlead = db.execute(
                            """
                            SELECT id, username, display_name, role,
                                   supervisor_specialty, team_name
                            FROM app_users WHERE id = ?
                            """,
                            (user_id,),
                        ).fetchone()
                self._send_json(200, {"user": _public_user(teamlead)})
            elif supervisor_specialty_match := re.fullmatch(
                r"/api/users/(\d+)/specialty", path
            ):
                payload = self._json_body()
                if not isinstance(payload, dict):
                    raise ApiError(400, "Supervisor details must be a JSON object.")
                specialty = payload.get("supervisor_specialty")
                if not isinstance(specialty, str):
                    raise ApiError(400, "Supervisor specialty must be text.")
                specialty = specialty.strip()
                if len(specialty) > 120:
                    raise ApiError(
                        400, "Supervisor specialty must be 120 characters or fewer."
                    )
                user_id = int(supervisor_specialty_match.group(1))
                with connect_db() as db:
                    with db:
                        supervisor = db.execute(
                            """
                            SELECT id, username, display_name, role,
                                   supervisor_specialty
                            FROM app_users WHERE id = ?
                            """,
                            (user_id,),
                        ).fetchone()
                        if supervisor is None:
                            raise ApiError(404, "Account was not found.")
                        if not _role_has_access(
                            db, supervisor["role"], "supervisor"
                        ):
                            raise ApiError(
                                409, "Only Supervisor accounts can have a specialty."
                            )
                        db.execute(
                            "UPDATE app_users SET supervisor_specialty = ? WHERE id = ?",
                            (specialty, user_id),
                        )
                        supervisor = db.execute(
                            """
                            SELECT id, username, display_name, role,
                                   supervisor_specialty
                            FROM app_users WHERE id = ?
                            """,
                            (user_id,),
                        ).fetchone()
                self._send_json(200, {"user": _public_user(supervisor)})
            elif path == "/api/team-roster":
                payload = self._json_body()
                if not isinstance(payload, dict):
                    raise ApiError(400, "Team member details must be a JSON object.")
                teamlead_id = payload.get("teamlead_id")
                if isinstance(teamlead_id, bool):
                    raise ApiError(400, "Choose an active Teamlead.")
                try:
                    teamlead_id = int(teamlead_id)
                except (TypeError, ValueError):
                    raise ApiError(400, "Choose an active Teamlead.") from None
                member_name = payload.get("member_name")
                if not isinstance(member_name, str):
                    raise ApiError(400, "Enter a valid team member name.")
                member_name = member_name.strip()
                if not member_name or len(member_name) > 120:
                    raise ApiError(
                        400, "Team member name must be between 1 and 120 characters."
                    )
                with connect_db() as db:
                    with db:
                        teamlead = db.execute(
                            """
                            SELECT id FROM app_users
                            WHERE id = ? AND is_active = 1
                              AND EXISTS (
                                  SELECT 1 FROM app_role_permissions access
                                  WHERE access.role_key = app_users.role
                                    AND access.permission_key = 'access.teamlead'
                                    AND access.is_allowed = 1
                              )
                            """,
                            (teamlead_id,),
                        ).fetchone()
                        if teamlead is None:
                            raise ApiError(400, "Choose an active Teamlead.")
                        if not _can_manage_team_roster(db, user, teamlead_id):
                            raise ApiError(
                                403,
                                "You can manage only your own roster or a Teamlead who reports to you.",
                            )
                        inserted = db.execute(
                            """
                            INSERT INTO team_roster_members(teamlead_user_id, member_name)
                            VALUES (?, ?)
                            ON CONFLICT(teamlead_user_id, member_name) DO NOTHING
                            """,
                            (teamlead_id, member_name),
                        )
                        if inserted.rowcount == 0:
                            raise ApiError(
                                409, "That team member is already on this roster."
                            )
                        member_id = inserted.lastrowid
                    rosters = _visible_team_rosters(db, user)
                self._send_json(
                    201,
                    {
                        "member": {"id": member_id, "name": member_name},
                        "team_rosters": rosters,
                    },
                )
            elif team_roster_delete_match := re.fullmatch(
                r"/api/team-roster/(\d+)/delete", path
            ):
                member_id = int(team_roster_delete_match.group(1))
                with connect_db() as db:
                    with db:
                        member = db.execute(
                            "SELECT teamlead_user_id FROM team_roster_members WHERE id = ?",
                            (member_id,),
                        ).fetchone()
                        if member is None:
                            raise ApiError(404, "Team member was not found.")
                        if not _can_manage_team_roster(
                            db, user, member["teamlead_user_id"]
                        ):
                            raise ApiError(
                                403,
                                "You can manage only your own roster or a Teamlead who reports to you.",
                            )
                        deleted = db.execute(
                            "DELETE FROM team_roster_members WHERE id = ?",
                            (member_id,),
                        )
                    rosters = _visible_team_rosters(db, user)
                self._send_json(200, {"ok": True, "team_rosters": rosters})
            elif path == "/api/supervisor/ewi-assignment":
                    payload = self._json_body()
                    if not isinstance(payload, dict) or set(payload) != {
                        "house_id",
                        "teamlead_user_id",
                    }:
                        raise ApiError(
                            400,
                            "Choose a house and an EWI Teamlead.",
                        )
                    if not isinstance(payload["house_id"], str):
                        raise ApiError(400, "Enter a valid house ID.")
                    house_id = payload["house_id"].strip()
                    if not house_id or len(house_id) > 120:
                        raise ApiError(400, "Enter a valid house ID.")
                    raw_teamlead_id = payload["teamlead_user_id"]
                    if raw_teamlead_id in ("", None):
                        teamlead_user_id = None
                    elif isinstance(raw_teamlead_id, bool):
                        raise ApiError(400, "Choose an active EWI Teamlead.")
                    else:
                        try:
                            teamlead_user_id = int(raw_teamlead_id)
                        except (TypeError, ValueError, OverflowError) as exc:
                            raise ApiError(
                                400, "Choose an active EWI Teamlead."
                            ) from exc
                    now = datetime.now().isoformat(timespec="seconds")
                    with connect_db() as db:
                        with db:
                            if not _is_ewi_supervisor(db, user):
                                raise ApiError(
                                    403,
                                    "External Wall Insulation Supervisor access is required to assign EWI work.",
                                )
                            assigned = db.execute(
                                """
                                SELECT 1
                                FROM house_role_assignments assignment
                                JOIN houses house ON house.id = assignment.house_id
                                WHERE assignment.house_id = ?
                                  AND assignment.role = 'supervisor'
                                  AND assignment.user_id = ?
                                  AND house.is_finished = 0
                                """,
                                (house_id, user["id"]),
                            ).fetchone()
                            if not assigned:
                                raise ApiError(
                                    404,
                                    "That active house is not assigned to your Supervisor account.",
                                )
                            teamlead = None
                            if teamlead_user_id is not None:
                                teamlead = db.execute(
                                    """
                                    SELECT users.id, users.display_name
                                    FROM app_users users
                                    JOIN user_reporting_lines reports
                                      ON reports.report_user_id = users.id
                                    JOIN user_departments memberships
                                      ON memberships.user_id = users.id
                                    JOIN departments
                                      ON departments.id = memberships.department_id
                                    WHERE users.id = ?
                                      AND users.is_active = 1
                                      AND reports.manager_user_id = ?
                                      AND departments.name = 'External Wall Insulation' COLLATE NOCASE
                                      AND departments.is_active = 1
                                      AND EXISTS (
                                          SELECT 1 FROM app_role_permissions access
                                          WHERE access.role_key = users.role
                                            AND access.permission_key = 'access.teamlead'
                                            AND access.is_allowed = 1
                                      )
                                    """,
                                    (teamlead_user_id, user["id"]),
                                ).fetchone()
                                if teamlead is None:
                                    raise ApiError(
                                        400,
                                        "Choose an active External Wall Insulation Teamlead who reports to you.",
                                    )
                                db.execute(
                                    """
                                    INSERT INTO house_ewi_assignments
                                        (house_id, teamlead_user_id, supervisor_user_id,
                                         assigned_by, assigned_at)
                                    VALUES (?, ?, ?, ?, ?)
                                    ON CONFLICT(house_id) DO UPDATE SET
                                        teamlead_user_id = excluded.teamlead_user_id,
                                        supervisor_user_id = excluded.supervisor_user_id,
                                        assigned_by = excluded.assigned_by,
                                        assigned_at = excluded.assigned_at,
                                        completed_by = '',
                                        completed_at = ''
                                    """,
                                    (
                                        house_id,
                                        teamlead_user_id,
                                        user["id"],
                                        user["display_name"],
                                        now,
                                    ),
                                )
                            else:
                                db.execute(
                                    "DELETE FROM house_ewi_assignments WHERE house_id = ?",
                                    (house_id,),
                                )
                    self._send_json(
                        200,
                        {
                            "house_id": house_id,
                            "teamlead_user_id": teamlead_user_id,
                            "teamlead_name": teamlead["display_name"] if teamlead else "",
                            "assigned_by": user["display_name"] if teamlead else "",
                            "assigned_at": now if teamlead else "",
                            "completed_by": "",
                            "completed_at": "",
                        },
                    )
            elif path == "/api/teamlead/ewi-work/complete":
                    payload = self._json_body()
                    if not isinstance(payload, dict) or set(payload) != {"house_id"}:
                        raise ApiError(400, "A house ID is required.")
                    if not isinstance(payload["house_id"], str):
                        raise ApiError(400, "Enter a valid house ID.")
                    house_id = payload["house_id"].strip()
                    if not house_id or len(house_id) > 120:
                        raise ApiError(400, "Enter a valid house ID.")
                    completed_at = datetime.now().isoformat(timespec="seconds")
                    with connect_db() as db:
                        with db:
                            assignment = db.execute(
                                """
                                SELECT assignment.completed_at, house.is_finished
                                FROM house_ewi_assignments assignment
                                JOIN houses house ON house.id = assignment.house_id
                                JOIN house_role_assignments supervisor_assignment
                                  ON supervisor_assignment.house_id = assignment.house_id
                                 AND supervisor_assignment.role = 'supervisor'
                                 AND supervisor_assignment.user_id =
                                     assignment.supervisor_user_id
                                JOIN user_reporting_lines reports
                                  ON reports.manager_user_id = assignment.supervisor_user_id
                                 AND reports.report_user_id = assignment.teamlead_user_id
                                WHERE assignment.house_id = ?
                                  AND assignment.teamlead_user_id = ?
                                """,
                                (house_id, user["id"]),
                            ).fetchone()
                            if assignment is None:
                                raise ApiError(
                                    404,
                                    "That house is not assigned to your EWI work queue.",
                                )
                            if assignment["is_finished"]:
                                raise ApiError(
                                    409,
                                    "This house is already closed and cannot be updated in your EWI work queue.",
                                )
                            if not assignment["completed_at"]:
                                db.execute(
                                    """
                                    UPDATE house_ewi_assignments
                                    SET completed_by = ?, completed_at = ?
                                    WHERE house_id = ? AND teamlead_user_id = ?
                                    """,
                                    (user["display_name"], completed_at, house_id, user["id"]),
                                )
                    self._send_json(
                        200,
                        {
                            "house_id": house_id,
                            "completed_by": user["display_name"],
                            "completed_at": assignment["completed_at"] or completed_at,
                        },
                    )
            elif path == "/api/teamlead/insulation-work/complete":
                    payload = self._json_body()
                    if not isinstance(payload, dict) or set(payload) != {
                        "house_id",
                        "work_type",
                    }:
                        raise ApiError(
                            400, "A house ID and insulation work type are required."
                        )
                    house_id = payload["house_id"]
                    work_type = payload["work_type"]
                    if (
                        not isinstance(house_id, str)
                        or not house_id.strip()
                        or len(house_id.strip()) > 120
                    ):
                        raise ApiError(400, "Enter a valid house ID.")
                    if work_type not in {"attic", "cavity"}:
                        raise ApiError(400, "Choose Attic or Cavity insulation work.")
                    house_id = house_id.strip()
                    completed_at = datetime.now().isoformat(timespec="seconds")
                    with connect_db() as db:
                        with db:
                            if not any(
                                assignment["id"] == house_id
                                and assignment["work_type"] == work_type
                                for assignment in _teamlead_insulation_houses(
                                    db, user["id"]
                                )
                            ):
                                raise ApiError(
                                    404,
                                    "That insulation job is not assigned to your work queue.",
                                )
                            assignment = db.execute(
                                """
                                SELECT completed_at, is_finished
                                FROM house_insulation_assignments assignment
                                LEFT JOIN houses ON houses.id = assignment.house_id
                                WHERE assignment.house_id = ?
                                  AND assignment.work_type = ?
                                  AND assignment.teamlead_user_id = ?
                                """,
                                (house_id, work_type, user["id"]),
                            ).fetchone()
                            if assignment["is_finished"]:
                                raise ApiError(
                                    409,
                                    "This house is closed and cannot be updated.",
                                )
                            if not assignment["completed_at"]:
                                db.execute(
                                    """
                                    UPDATE house_insulation_assignments
                                    SET completed_by = ?, completed_at = ?
                                    WHERE house_id = ? AND work_type = ?
                                      AND teamlead_user_id = ?
                                    """,
                                    (
                                        user["display_name"],
                                        completed_at,
                                        house_id,
                                        work_type,
                                        user["id"],
                                    ),
                                )
                    self._send_json(
                        200,
                        {
                            "house_id": house_id,
                            "work_type": work_type,
                            "completed_by": user["display_name"],
                            "completed_at": assignment["completed_at"]
                            or completed_at,
                        },
                    )
            elif path == "/api/supervisor/house-assignment":
                payload = self._json_body()
                if not isinstance(payload, dict) or set(payload) != {
                    "house_id",
                    "delivery_person",
                    "delivery_team",
                }:
                    raise ApiError(
                        400,
                        "House ID, delivery person, and team are required.",
                    )
                if not isinstance(payload["house_id"], str):
                    raise ApiError(400, "Enter a valid house ID.")
                house_id = payload["house_id"].strip()
                if not house_id or len(house_id) > 120:
                    raise ApiError(400, "Enter a valid house ID.")
                delivery_person = payload["delivery_person"]
                delivery_team = payload["delivery_team"]
                if not isinstance(delivery_person, str) or not isinstance(
                    delivery_team, str
                ):
                    raise ApiError(
                        400, "Delivery person and team must be text."
                    )
                delivery_person = delivery_person.strip()
                delivery_team = delivery_team.strip()
                if len(delivery_person) > 120 or len(delivery_team) > 120:
                    raise ApiError(
                        400,
                        "Delivery person and team must be 120 characters or fewer.",
                    )
                with connect_db() as db:
                    with db:
                        assigned = db.execute(
                            """
                            SELECT 1
                            FROM house_role_assignments assignment
                            JOIN houses house ON house.id = assignment.house_id
                            WHERE assignment.house_id = ?
                              AND assignment.role = 'supervisor'
                              AND assignment.user_id = ?
                            """,
                            (house_id, user["id"]),
                        ).fetchone()
                        if not assigned:
                            raise ApiError(
                                404,
                                "That house is not assigned to your Supervisor account.",
                            )
                        db.execute(
                            """
                            UPDATE houses
                            SET delivery_person = ?, delivery_team = ?
                            WHERE id = ?
                            """,
                            (delivery_person, delivery_team, house_id),
                        )
                self._send_json(
                    200,
                    {
                        "house_id": house_id,
                        "delivery_person": delivery_person,
                        "delivery_team": delivery_team,
                    },
                )
            elif path == "/api/house-assignments":
                payload = self._json_body()
                if not isinstance(payload, dict):
                    raise ApiError(400, "House assignments must be a JSON object.")
                house_id = str(payload.get("house_id") or "").strip()
                if not house_id:
                    raise ApiError(400, "A house ID is required.")
                if set(payload) != {"house_id", "surveyor_user_id"}:
                    raise ApiError(400, "Choose one active Surveyor account.")
                try:
                    if isinstance(payload["surveyor_user_id"], bool):
                        raise ValueError
                    surveyor_user_id = int(payload["surveyor_user_id"])
                except (TypeError, ValueError, OverflowError) as exc:
                    raise ApiError(400, "Choose a valid Surveyor account.") from exc
                now = datetime.now().isoformat(timespec="seconds")
                with connect_db() as db:
                    with db:
                        available = db.execute(
                            """
                            SELECT 1 FROM provided_houses p
                            LEFT JOIN houses h ON h.id = p.id
                            WHERE p.id = ?
                              AND (
                                  h.id IS NULL
                                  OR (p.ready_for_start = 1 AND h.is_finished = 0)
                              )
                            """,
                            (house_id,),
                        ).fetchone()
                        if not available:
                            raise ApiError(409, "Assignments can only be changed for houses that are not active.")
                        assigned_user = db.execute(
                            """
                            SELECT id FROM app_users
                            WHERE id = ? AND is_active = 1
                              AND EXISTS (
                                  SELECT 1 FROM app_role_permissions access
                                  WHERE access.role_key = app_users.role
                                    AND access.permission_key = 'access.surveyor'
                                    AND access.is_allowed = 1
                              )
                            """,
                            (surveyor_user_id,),
                        ).fetchone()
                        if not assigned_user:
                            raise ApiError(400, "Choose an active Surveyor account.")
                        db.execute(
                            """
                            INSERT INTO house_role_assignments
                                (house_id, role, user_id, assigned_by, assigned_at)
                            VALUES (?, 'surveyor', ?, ?, ?)
                            ON CONFLICT(house_id, role) DO UPDATE SET
                                user_id = excluded.user_id,
                                assigned_by = excluded.assigned_by,
                                assigned_at = excluded.assigned_at
                            """,
                            (
                                house_id,
                                surveyor_user_id,
                                user["display_name"],
                                now,
                            ),
                        )
                self._send_json(200, {"house_id": house_id, "saved": True})
            elif path in {"/api/movements", "/api/movements/batch"}:
                payload = self._json_body()
                if not isinstance(payload, dict):
                    raise ApiError(400, "Movement data must be a JSON object.")
                allow_house_updates = _has_access_profile(user, "admin")
                if path.endswith("/batch"):
                    with connect_db() as db:
                        with db:
                            db.execute("BEGIN IMMEDIATE")
                            movement_ids, delivery_number = _record_movement_batch(
                                db, payload, allow_house_updates
                            )
                else:
                    if not isinstance(payload, dict):
                        raise ApiError(400, "Movement data must be a JSON object.")
                    with connect_db() as db:
                        with db:
                            db.execute("BEGIN IMMEDIATE")
                            movement_ids = [
                                _record_movement(
                                    db,
                                    payload,
                                    allow_house_updates=allow_house_updates,
                                )
                            ]
                            delivery_number = None
                            if payload.get("kind") == "delivery":
                                delivery_number = db.execute(
                                    "SELECT delivery_number FROM movements WHERE id = ?",
                                    (movement_ids[0],),
                                ).fetchone()[0]
                if path.endswith("/batch"):
                    self._send_json(
                        201,
                        {"ids": movement_ids, "delivery_number": delivery_number},
                    )
                else:
                    self._send_json(
                        201,
                        {"id": movement_ids[0], "delivery_number": delivery_number},
                    )
            elif path == "/api/plumbing-status":
                payload = self._json_body()
                if (
                    not isinstance(payload, dict)
                    or set(payload) != {"house_id", "plumber_status"}
                ):
                    raise ApiError(
                        400, "House ID and plumbing status are required."
                    )
                if not _has_access_profile(user, "supervisor"):
                    raise ApiError(
                        403,
                        "Only Plumbing Supervisors can update assigned plumbing work.",
                    )
                house_id = payload["house_id"]
                plumber_status = payload["plumber_status"]
                if not isinstance(house_id, str) or not house_id.strip():
                    raise ApiError(400, "Enter a valid house ID.")
                house_id = house_id.strip()
                if len(house_id) > 120:
                    raise ApiError(400, "Enter a valid house ID.")
                if (
                    not isinstance(plumber_status, str)
                    or plumber_status not in {"done", "not_done"}
                ):
                    raise ApiError(
                        400, "Plumbing status must be complete or outstanding."
                    )
                with connect_db() as db:
                    with db:
                        db.execute("BEGIN IMMEDIATE")
                        if not _is_plumbing_supervisor(db, user):
                            raise ApiError(
                                403,
                                "Only Plumbing Supervisors can update assigned plumbing work.",
                            )
                        assigned = db.execute(
                            """
                            SELECT 1
                            FROM provided_houses provided
                            WHERE provided.id = ?
                              AND provided.plan_plumber_required = 1
                              AND (
                                  EXISTS (
                                      SELECT 1 FROM house_plumbing_assignments assignment
                                      WHERE assignment.house_id = provided.id
                                        AND assignment.supervisor_user_id = ?
                                  )
                                  OR EXISTS (
                                      SELECT 1 FROM house_role_assignments assignment
                                      WHERE assignment.house_id = provided.id
                                        AND assignment.role = 'supervisor'
                                        AND assignment.user_id = ?
                                  )
                              )
                            """,
                            (house_id, user["id"], user["id"]),
                        ).fetchone()
                        if not assigned:
                            raise ApiError(
                                404,
                                "That plumbing house is not assigned to you.",
                            )
                        _require_work_plan_checker(
                            db, user, house_id, "plumber"
                        )
                        if (
                            plumber_status == "done"
                            and db.execute(
                                """
                                SELECT 1 FROM house_plumbing_photos
                                WHERE house_id = ? LIMIT 1
                                """,
                                (house_id,),
                            ).fetchone()
                            is None
                        ):
                            raise ApiError(
                                409,
                                "Upload at least one Plumbing team photo before marking plumbing complete.",
                            )
                        db.execute(
                            """
                            INSERT OR IGNORE INTO house_checklists (house_id)
                            VALUES (?)
                            """,
                            (house_id,),
                        )
                        db.execute(
                            """
                            UPDATE house_checklists
                            SET plumber_status = ?
                            WHERE house_id = ?
                            """,
                            (plumber_status, house_id),
                        )
                        completed_at = (
                            datetime.now().isoformat(timespec="seconds")
                            if plumber_status == "done"
                            else ""
                        )
                        db.execute(
                            """
                            UPDATE provided_houses
                            SET plan_plumber_done_by = ?,
                                plan_plumber_done_at = ?,
                                plan_plumber_done_by_user_id = ?
                            WHERE id = ?
                            """,
                            (
                                user["display_name"] if completed_at else "",
                                completed_at,
                                user["id"] if completed_at else None,
                                house_id,
                            ),
                        )
                self._send_json(
                    200,
                    {
                        "house_id": house_id,
                        "plumber_status": plumber_status,
                        "plumber_done_by": (
                            user["display_name"] if plumber_status == "done" else ""
                        ),
                        "plumber_done_at": completed_at,
                        "plumber_done_by_user_id": (
                            user["id"] if plumber_status == "done" else None
                        ),
                        "saved": True,
                    },
                )
            elif path == "/api/house-checklist":
                payload = self._json_body()
                if not isinstance(payload, dict):
                    raise ApiError(400, "House checklist must be a JSON object.")
                is_assign = _has_access_profile(user, "assign")
                if not any(_has_access_profile(user, role) for role in ("admin", "employee", "plumber", "assign")):
                    raise ApiError(403, "Administrator or employee access is required.")
                house_id = str(payload.get("house_id") or "").strip()
                if not house_id:
                    raise ApiError(400, "House ID is required.")
                status_options = {
                    "scaffolding_status": {"done", "not_done", "not_necessary"},
                    "esb_unclipped": {"yes", "no", "not_esb"},
                    "plumber_status": {"done", "not_done", "not_necessary"},
                }
                supplied_statuses = {
                    name: payload[name] for name in status_options if name in payload
                }
                if not supplied_statuses:
                    raise ApiError(400, "Choose at least one house checklist status.")
                if is_assign and not set(supplied_statuses).issubset(
                    {"scaffolding_status", "esb_unclipped"}
                ):
                    raise ApiError(
                        403,
                        "Assign access is limited to scaffolding and ESB checklist items.",
                    )
                for name, value in supplied_statuses.items():
                    if (
                        not isinstance(value, str)
                        or value not in status_options[name]
                    ):
                        raise ApiError(
                            400, f"Choose a valid {name.replace('_', ' ')}."
                        )
                with connect_db() as db:
                    with db:
                        db.execute("BEGIN IMMEDIATE")
                        for checklist_field in supplied_statuses:
                            trade = {
                                "scaffolding_status": "scaffolding",
                                "esb_unclipped": "esb",
                                "plumber_status": "plumber",
                            }[checklist_field]
                            _require_work_plan_checker(
                                db, user, house_id, trade
                            )
                        is_active = db.execute(
                            "SELECT is_finished FROM houses WHERE id = ?", (house_id,)
                        ).fetchone()
                        if is_assign and (
                            not is_active or bool(is_active["is_finished"])
                        ):
                            raise ApiError(
                                403,
                                "Assign access can update checklists only on active houses.",
                            )
                        if not is_active:
                            if not _has_access_profile(user, "admin"):
                                raise ApiError(
                                    403,
                                    "Administrator access is required to edit a pre-start checklist.",
                                )
                            if set(supplied_statuses) != set(status_options):
                                raise ApiError(
                                    400, "Choose all pre-start checklist statuses."
                                )
                            planned = db.execute(
                                "UPDATE provided_houses SET plan_scaffolding = ?, "
                                "plan_esb = ?, plan_plumber = ?, "
                                "plan_plumber_done_by = ?, plan_plumber_done_at = ?, "
                                "plan_plumber_done_by_user_id = ? "
                                "WHERE id = ?",
                                (
                                    supplied_statuses["scaffolding_status"],
                                    supplied_statuses["esb_unclipped"],
                                    supplied_statuses["plumber_status"],
                                    user["display_name"]
                                    if supplied_statuses["plumber_status"] == "done"
                                    else "",
                                    datetime.now().isoformat(timespec="seconds")
                                    if supplied_statuses["plumber_status"] == "done"
                                    else "",
                                    user["id"]
                                    if supplied_statuses["plumber_status"] == "done"
                                    else None,
                                    house_id,
                                ),
                            )
                            if planned.rowcount == 0:
                                raise ApiError(404, "House was not found.")
                            plumber_done = (
                                supplied_statuses["plumber_status"] == "done"
                            )
                            self._send_json(
                                200,
                                {
                                    "house_id": house_id,
                                    "saved": True,
                                    "plumber_done_by": (
                                        user["display_name"] if plumber_done else ""
                                    ),
                                    "plumber_done_at": (
                                        db.execute(
                                            "SELECT plan_plumber_done_at "
                                            "FROM provided_houses WHERE id = ?",
                                            (house_id,),
                                        ).fetchone()["plan_plumber_done_at"]
                                    ),
                                    "plumber_done_by_user_id": (
                                        user["id"] if plumber_done else None
                                    ),
                                },
                            )
                            return
                        completion_updates = {}
                        if is_assign:
                            required_columns = {
                                "scaffolding_status": "plan_scaffolding_required",
                                "esb_unclipped": "plan_esb_required",
                            }
                            for status_name, column_name in required_columns.items():
                                if status_name not in supplied_statuses:
                                    continue
                                required = db.execute(
                                    f"SELECT {column_name} FROM provided_houses WHERE id = ?",
                                    (house_id,),
                                ).fetchone()
                                value = supplied_statuses[status_name]
                                is_completed_or_pending = value in {
                                    "done", "not_done", "yes", "no"
                                }
                                if (
                                    required is None
                                    or required[column_name] != 1
                                    or not is_completed_or_pending
                                ):
                                    raise ApiError(
                                        403,
                                        "Assign access can update only required scaffolding or ESB work.",
                                    )
                        db.execute(
                            "INSERT OR IGNORE INTO house_checklists (house_id) VALUES (?)",
                            (house_id,),
                        )
                        for name, value in supplied_statuses.items():
                            db.execute(
                                f"UPDATE house_checklists SET {name} = ? WHERE house_id = ?",
                                (value, house_id),
                            )
                            trade = {
                                "scaffolding_status": "scaffolding",
                                "esb_unclipped": "esb",
                                "plumber_status": "plumber",
                            }[name]
                            completed = value in {"done", "yes"}
                            completed_at = (
                                datetime.now().isoformat(timespec="seconds")
                                if completed
                                else ""
                            )
                            completed_by = user["display_name"] if completed else ""
                            db.execute(
                                f"UPDATE provided_houses SET plan_{trade}_done_by = ?, "
                                f"plan_{trade}_done_at = ?, "
                                f"plan_{trade}_done_by_user_id = ? WHERE id = ?",
                                (
                                    completed_by,
                                    completed_at,
                                    user["id"] if completed else None,
                                    house_id,
                                ),
                            )
                            completion_updates[f"{trade}_done_by"] = completed_by
                            completion_updates[f"{trade}_done_at"] = completed_at
                            completion_updates[f"{trade}_done_by_user_id"] = (
                                user["id"] if completed else None
                            )
                self._send_json(
                    200,
                    {
                        "house_id": house_id,
                        "saved": True,
                        **completion_updates,
                    },
                )
            elif path == "/api/house-work-plan":
                if not (_has_access_profile(user, "admin") or _has_access_profile(user, "surveyor")):
                    raise ApiError(
                        403, "Surveyor access is required to save a work plan."
                    )
                payload = self._json_body()
                if not isinstance(payload, dict):
                    raise ApiError(400, "House work plan must be a JSON object.")
                house_id = str(payload.get("house_id") or "").strip()
                if not house_id:
                    raise ApiError(400, "House ID is required.")
                required_values = {}
                notes_values = {}
                question_values = {}
                for trade in WORK_PLAN_TRADES:
                    required = payload.get(f"{trade}_required")
                    if required is not None and not isinstance(required, bool):
                        raise ApiError(
                            400, f"Choose whether {trade} work is required."
                        )
                    notes = payload.get(f"{trade}_notes", "")
                    if not isinstance(notes, str) or len(notes) > 1000:
                        raise ApiError(
                            400,
                            f"{trade.title()} work notes must be 1,000 characters or fewer.",
                        )
                    required_values[trade] = (
                        None if required is None else int(required)
                    )
                    notes_values[trade] = notes.strip()
                for question in WORK_PLAN_SURVEY_QUESTIONS:
                    answer = payload.get(question)
                    if answer is not None and not isinstance(answer, bool):
                        raise ApiError(
                            400,
                            "Choose yes or no for each pre-start survey question.",
                        )
                    question_values[question] = (
                        None if answer is None else int(answer)
                    )
                if any(
                    required_values[trade] is None
                    for trade in ("scaffolding", "esb", "plumber")
                ):
                    raise ApiError(
                        400,
                        "Choose whether Scaffolding, ESB, and Plumber work is required before saving the work plan.",
                    )
                draft_data = {}
                for trade in WORK_PLAN_TRADES:
                    draft_data[f"{trade}_required"] = (
                        None
                        if required_values[trade] is None
                        else bool(required_values[trade])
                    )
                    draft_data[f"{trade}_notes"] = notes_values[trade]
                for question in WORK_PLAN_SURVEY_QUESTIONS:
                    answer = question_values[question]
                    draft_data[question] = (
                        None if answer is None else bool(answer)
                    )
                assignments = ", ".join(
                    f"plan_{trade}_required = ?, plan_{trade}_notes = ?, "
                    f"plan_{trade}_done_by = CASE WHEN ? = 1 THEN plan_{trade}_done_by ELSE '' END, "
                    f"plan_{trade}_done_at = CASE WHEN ? = 1 THEN plan_{trade}_done_at ELSE '' END, "
                    f"plan_{trade}_done_by_user_id = CASE WHEN ? = 1 THEN plan_{trade}_done_by_user_id ELSE NULL END"
                    for trade in WORK_PLAN_TRADES
                )
                assignments += ", " + ", ".join(
                    f"plan_{question} = ?, "
                    f"plan_{question}_done_by = CASE "
                    f"WHEN plan_{question} IS ? THEN plan_{question}_done_by "
                    "ELSE '' END, "
                    f"plan_{question}_done_at = CASE "
                    f"WHEN plan_{question} IS ? THEN plan_{question}_done_at "
                    "ELSE '' END, "
                    f"plan_{question}_done_by_user_id = CASE "
                    f"WHEN plan_{question} IS ? THEN plan_{question}_done_by_user_id "
                    "ELSE NULL END"
                    for question in WORK_PLAN_SURVEY_QUESTIONS
                )
                values = []
                for trade in WORK_PLAN_TRADES:
                    required = required_values[trade]
                    values.extend(
                        (
                            required,
                            notes_values[trade],
                            required or 0,
                            required or 0,
                            required or 0,
                        )
                    )
                for question in WORK_PLAN_SURVEY_QUESTIONS:
                    answer = question_values[question]
                    values.extend((answer, answer, answer, answer))
                with connect_db() as db:
                    with db:
                        saved = db.execute(
                            """
                            SELECT 1 FROM provided_houses
                            WHERE id = ? AND ready_for_start = 1
                              AND NOT EXISTS (SELECT 1 FROM houses WHERE houses.id = ?)
                            """,
                            (house_id, house_id),
                        )
                        if saved.fetchone() is None:
                            exists = db.execute(
                                "SELECT 1 FROM provided_houses WHERE id = ?",
                                (house_id,),
                            ).fetchone()
                            if exists is None:
                                raise ApiError(404, "House was not found.")
                            raise ApiError(
                                409,
                                "Work plans can only be edited for Ready for start houses.",
                            )
                        saved_at = datetime.now().isoformat(timespec="seconds")
                        published = _has_access_profile(user, "admin")
                        if published:
                            db.execute(
                                f"UPDATE provided_houses SET {assignments} WHERE id = ?",
                                (*values, house_id),
                            )
                            db.execute(
                                """
                                DELETE FROM house_vent_assignments
                                WHERE house_id = ?
                                  AND NOT EXISTS (
                                      SELECT 1 FROM provided_houses
                                      WHERE id = ? AND plan_vent_required = 1
                                  )
                                """,
                                (house_id, house_id),
                            )
                            db.execute(
                                "DELETE FROM house_work_plan_drafts WHERE house_id = ?",
                                (house_id,),
                            )
                        else:
                            db.execute(
                                """
                                INSERT INTO house_work_plan_drafts
                                    (house_id, data, saved_by, saved_at)
                                VALUES (?, ?, ?, ?)
                                ON CONFLICT(house_id) DO UPDATE SET
                                    data = excluded.data,
                                    saved_by = excluded.saved_by,
                                    saved_at = excluded.saved_at
                                """,
                                (
                                    house_id,
                                    json.dumps(draft_data, ensure_ascii=False),
                                    user["display_name"],
                                    saved_at,
                                ),
                            )
                self._send_json(
                    200,
                    {
                        "house_id": house_id,
                        "saved": True,
                        "published": published,
                        "saved_at": saved_at,
                    },
                )
            elif path == "/api/house-work-plan/complete":
                payload = self._json_body()
                if not isinstance(payload, dict):
                    raise ApiError(400, "Work plan update must be a JSON object.")
                house_id = str(payload.get("house_id") or "").strip()
                item = payload.get("item", payload.get("trade"))
                done = payload.get("done")
                if not house_id:
                    raise ApiError(400, "House ID is required.")
                if item not in (*WORK_PLAN_TRADES, *WORK_PLAN_SURVEY_QUESTIONS):
                    raise ApiError(400, "Choose a valid work plan item.")
                if not isinstance(done, bool):
                    raise ApiError(400, "Choose whether the work is done.")
                is_plumbing_coordinator = _has_access_profile(
                    user, "plumbing_coordinator"
                )
                is_ventilation_coordinator = _has_access_profile(
                    user, "ventilation_coordinator"
                )
                with connect_db() as db:
                    with db:
                        db.execute("BEGIN IMMEDIATE")
                        if is_plumbing_coordinator:
                            if not _plumbing_coordinator_house_is_visible(
                                db, house_id
                            ):
                                raise ApiError(
                                    403,
                                    "This house is outside your plumbing work queue.",
                                )
                            if item in {"plumber", "vent"}:
                                raise ApiError(
                                    403,
                                    "Plumbing and Vent work must be checked from the appropriate team dashboard.",
                                )
                        elif is_ventilation_coordinator:
                            if not _ventilation_coordinator_house_is_visible(
                                db, house_id
                            ):
                                raise ApiError(
                                    403,
                                    "This house is outside your Vent work queue.",
                                )
                            if item in {"plumber", "vent"}:
                                raise ApiError(
                                    403,
                                    "Plumbing and Vent work must be checked from the appropriate team dashboard.",
                                )
                        elif item in WORK_PLAN_SURVEY_QUESTIONS:
                            if not (
                                _has_access_profile(user, "assign")
                                or _has_access_profile(user, "surveyor")
                            ):
                                raise ApiError(
                                    403,
                                    "Only Assign or Surveyor access can check pre-start survey questions.",
                                )
                            if (
                                _has_access_profile(user, "surveyor")
                                and not _has_access_profile(user, "assign")
                                and not db.execute(
                                    """
                                    SELECT 1
                                    FROM house_role_assignments
                                    WHERE house_id = ?
                                      AND role = 'surveyor'
                                      AND user_id = ?
                                    """,
                                    (house_id, user["id"]),
                                ).fetchone()
                            ):
                                raise ApiError(
                                    403,
                                    "This house is not assigned to your Surveyor account.",
                                )
                        elif not _can_complete_work_plan_trade(db, user, item):
                            raise ApiError(
                                403,
                                "Plumber work can only be checked by a Plumber."
                                if item == "plumber"
                                else "Vent work can only be checked by a Vent team account."
                                if item == "vent"
                                else "Only Assign, External Wall Insulation supervisors and team leads can check this item.",
                            )
                        completion = db.execute(
                            f"""
                            SELECT plan_{item}_done_by AS done_by,
                                   plan_{item}_done_at AS done_at,
                                   plan_{item}_done_by_user_id AS done_by_user_id
                            FROM provided_houses
                            WHERE id = ?
                            """,
                            (house_id,),
                        ).fetchone()
                        if completion and (
                            completion["done_by"] or completion["done_at"]
                        ) and completion["done_by_user_id"] != user["id"]:
                            raise ApiError(
                                403,
                                "Only the original checker can change this work-plan status.",
                            )
                        if (
                            item == "vent"
                            and done
                            and _is_vent_photo_uploader(user)
                            and not db.execute(
                                "SELECT 1 FROM house_vent_photos WHERE house_id = ? LIMIT 1",
                                (house_id,),
                            ).fetchone()
                        ):
                            raise ApiError(
                                409,
                                "Upload at least one Vent team photo before marking Vent work done.",
                            )
                        completed_at = datetime.now().isoformat(timespec="seconds")
                        if item in WORK_PLAN_SURVEY_QUESTIONS:
                            saved = db.execute(
                                f"""
                                UPDATE provided_houses
                                SET plan_{item}_done_by = ?,
                                    plan_{item}_done_at = ?,
                                    plan_{item}_done_by_user_id = ?
                                WHERE id = ?
                                  AND plan_{item} IS NOT NULL
                                  AND (? = 1 OR plan_{item}_done_at != '')
                                  AND (? = 0 OR plan_{item} = 1)
                                  AND (
                                      ? = 1
                                      OR (
                                          ready_for_start = 1
                                          AND NOT EXISTS (
                                              SELECT 1 FROM houses
                                              WHERE houses.id = provided_houses.id
                                                AND houses.is_finished = 1
                                          )
                                      )
                                  )
                                  AND (
                                      (
                                          COALESCE(plan_{item}_done_by, '') = ''
                                          AND COALESCE(plan_{item}_done_at, '') = ''
                                      )
                                      OR plan_{item}_done_by_user_id = ?
                                  )
                                """,
                                (
                                    user["display_name"] if done else "",
                                    completed_at if done else "",
                                    user["id"] if done else None,
                                    house_id,
                                    int(done),
                                    int(done),
                                    int(
                                        is_plumbing_coordinator
                                        or is_ventilation_coordinator
                                    ),
                                    user["id"],
                                ),
                            )
                        else:
                            saved = db.execute(
                                f"""
                                UPDATE provided_houses
                                SET plan_{item}_done_by = ?,
                                    plan_{item}_done_at = ?,
                                    plan_{item}_done_by_user_id = ?
                                WHERE id = ?
                                  AND plan_{item}_required = 1
                                  AND (? = 1 OR plan_{item}_done_at != '')
                                  AND (
                                      ? = 1
                                      OR (
                                          (
                                              ready_for_start = 1
                                              AND NOT EXISTS (
                                                  SELECT 1 FROM houses
                                                  WHERE houses.id = provided_houses.id
                                                    AND houses.is_finished = 1
                                              )
                                          )
                                          OR (
                                              ? = 'vent'
                                              AND EXISTS (
                                                  SELECT 1 FROM houses
                                                  WHERE houses.id = provided_houses.id
                                                    AND houses.is_finished = 0
                                              )
                                              AND EXISTS (
                                                  SELECT 1 FROM house_vent_assignments assignment
                                                  WHERE assignment.house_id = provided_houses.id
                                                    AND assignment.teamlead_user_id = ?
                                              )
                                          )
                                      )
                                  )
                                  AND (
                                      (
                                          COALESCE(plan_{item}_done_by, '') = ''
                                          AND COALESCE(plan_{item}_done_at, '') = ''
                                      )
                                      OR plan_{item}_done_by_user_id = ?
                                  )
                                """,
                                (
                                    user["display_name"] if done else "",
                                    completed_at if done else "",
                                    user["id"] if done else None,
                                    house_id,
                                    int(done),
                                    int(
                                        is_plumbing_coordinator
                                        or is_ventilation_coordinator
                                    ),
                                    item,
                                    user["id"],
                                    user["id"],
                                ),
                            )
                        if saved.rowcount == 0:
                            raise ApiError(
                                409,
                                "Only Yes answers on Ready for start houses can be checked."
                                if item in WORK_PLAN_SURVEY_QUESTIONS
                                else "Only required work on Ready for start houses or assigned active Vent houses can be checked.",
                            )
                self._send_json(
                    200,
                    {
                        "house_id": house_id,
                        "item": item,
                        "trade": item,
                        "done_by": user["display_name"] if done else "",
                        "done_at": completed_at if done else "",
                        "done_by_user_id": user["id"] if done else None,
                    },
                )
            elif path == "/api/seai-measure-variation":
                if not (_has_access_profile(user, "admin") or _has_access_profile(user, "surveyor")):
                    raise ApiError(
                        403, "Administrator or surveyor access is required to edit SEAI quantities."
                    )
                payload = self._json_body()
                if not isinstance(payload, dict):
                    raise ApiError(400, "SEAI measure update must be a JSON object.")
                house_id = str(payload.get("house_id") or "").strip()
                measure_index = payload.get("measure_index")
                variation = payload.get("variation")
                if not house_id:
                    raise ApiError(400, "House ID is required.")
                if (
                    not isinstance(measure_index, int)
                    or isinstance(measure_index, bool)
                    or measure_index < 0
                ):
                    raise ApiError(400, "Choose a valid SEAI measure.")
                if not isinstance(variation, str):
                    raise ApiError(400, "Enter a valid variation quantity.")
                variation = variation.strip()
                if len(variation) > 80 or not re.fullmatch(
                    r"(?:\d+(?:\.\d{1,4})?)?", variation
                ):
                    raise ApiError(
                        400, "Variation quantity must be a non-negative number with up to 4 decimal places."
                    )
                if variation and float(variation) > 1_000_000_000:
                    raise ApiError(400, "Variation quantity is too large.")
                with connect_db() as db:
                    with db:
                        db.execute("BEGIN IMMEDIATE")
                        house = db.execute(
                            """
                            SELECT p.ready_for_start,
                                   CASE WHEN h.id IS NULL THEN 0 ELSE 1 END AS is_active
                            FROM provided_houses p
                            LEFT JOIN houses h ON h.id = p.id
                            WHERE p.id = ?
                            """,
                            (house_id,),
                        ).fetchone()
                        if house is None:
                            raise ApiError(404, "House was not found.")
                        if not house["ready_for_start"]:
                            raise ApiError(
                                409,
                                "Survey variations can only be edited for houses ready for start.",
                            )
                        if _has_access_profile(user, "surveyor") and (
                            house["is_active"]
                        ):
                            raise ApiError(
                                409,
                                "Surveyors can only edit quantities for Ready for start houses.",
                            )
                        if not db.execute(
                            "SELECT 1 FROM sqlite_master WHERE type = 'table' "
                            "AND name = 'seai_applications'"
                        ).fetchone():
                            raise ApiError(404, "SEAI application details were not found.")
                        application = db.execute(
                            "SELECT detail_data_json FROM seai_applications "
                            "WHERE application_id = ?",
                            (house_id,),
                        ).fetchone()
                        if application is None:
                            raise ApiError(404, "SEAI application details were not found.")
                        try:
                            details = json.loads(application["detail_data_json"])
                        except (TypeError, json.JSONDecodeError):
                            raise ApiError(500, "Stored SEAI application details are invalid.")
                        measures = details.get("Measures") if isinstance(details, dict) else None
                        if (
                            not isinstance(measures, list)
                            or measure_index >= len(measures)
                            or not isinstance(measures[measure_index], dict)
                        ):
                            raise ApiError(404, "SEAI measure was not found.")
                        measure = measures[measure_index]
                        previous_value = measure.get("Survey Qty including Variation")
                        survey_value = measure.get("Survey Quantity")
                        previous_variation = (
                            "" if previous_value is None else str(previous_value).strip()
                        )
                        survey_quantity = (
                            "" if survey_value is None else str(survey_value).strip()
                        )
                        changed_at = datetime.now().isoformat(timespec="seconds")
                        if previous_variation != variation:
                            db.execute(
                                """
                                INSERT INTO seai_measure_variation_history
                                    (house_id, measure_index, measure_name,
                                     survey_quantity, previous_variation, variation,
                                     changed_by, changed_by_username, changed_at)
                                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                                """,
                                (
                                    house_id,
                                    measure_index,
                                    str(measure.get("Name") or ""),
                                    survey_quantity,
                                    previous_variation,
                                    variation,
                                    user["display_name"],
                                    user["username"],
                                    changed_at,
                                ),
                            )
                        measures[measure_index]["Survey Qty including Variation"] = variation
                        db.execute(
                            "UPDATE seai_applications SET detail_data_json = ? "
                            "WHERE application_id = ?",
                            (json.dumps(details, ensure_ascii=False), house_id),
                        )
                self._send_json(
                    200,
                    {
                        "house_id": house_id,
                        "measure_index": measure_index,
                        "variation": variation,
                        "seai_details": details,
                    },
                )
            elif path == "/api/houses/snags":
                user = self._login_required()
                if user is None:
                    return
                payload = self._json_body()
                if not isinstance(payload, dict):
                    raise ApiError(400, "Snag details must be a JSON object.")
                with connect_db() as db:
                    with db:
                        if "snag_id" in payload:
                            snag = db.execute(
                                "SELECT id, house_id FROM house_snags WHERE id = ?",
                                (payload.get("snag_id"),),
                            ).fetchone()
                            if snag is None:
                                raise ApiError(404, "Snag was not found.")
                            if payload.get("delete") is True:
                                db.execute("DELETE FROM house_snags WHERE id = ?", (snag["id"],))
                            elif isinstance(payload.get("done"), bool):
                                db.execute(
                                    "UPDATE house_snags SET done = ? WHERE id = ?",
                                    (int(payload["done"]), snag["id"]),
                                )
                            else:
                                raise ApiError(400, "Choose whether the snag is done.")
                            house_id = snag["house_id"]
                        else:
                            house_id = str(payload.get("id") or "").strip()
                            text = str(payload.get("text") or "").strip()
                            if not text:
                                raise ApiError(400, "Describe the snag first.")
                            if len(text) > 500:
                                raise ApiError(400, "A snag must be 500 characters or fewer.")
                            if not db.execute(
                                "SELECT 1 FROM provided_houses WHERE id = ?", (house_id,)
                            ).fetchone():
                                raise ApiError(404, "House was not found.")
                            db.execute(
                                "INSERT INTO house_snags(house_id, text, created_at) VALUES (?, ?, ?)",
                                (house_id, text, datetime.now().timestamp()),
                            )
                    snags = _snags_by_house(db).get(house_id, [])
                self._send_json(200, {"id": house_id, "snags": snags})
            elif path == "/api/houses/stage":
                user = self._login_required()
                if user is None:
                    return
                payload = self._json_body()
                if not isinstance(payload, dict):
                    raise ApiError(400, "Stage details must be a JSON object.")
                house_id = str(payload.get("id") or "").strip()
                stage = payload.get("stage")
                if isinstance(stage, bool) or stage not in (1, 2):
                    raise ApiError(400, "Stage must be 1 or 2.")
                with connect_db() as db:
                    with db:
                        house = db.execute(
                            "SELECT stage FROM houses WHERE id = ?", (house_id,)
                        ).fetchone()
                        if house is None:
                            raise ApiError(404, "House was not found.")
                        if house["stage"] != stage:
                            db.execute(
                                "UPDATE houses SET stage = ? WHERE id = ?",
                                (stage, house_id),
                            )
                self._send_json(
                    200, {"id": house_id, "stage": stage}
                )
            elif path == "/api/houses/plan":
                user = self._login_required()
                if user is None:
                    return
                payload = self._json_body()
                if not isinstance(payload, dict):
                    raise ApiError(400, "House details must be a JSON object.")
                if _has_access_profile(user, "assign") and (
                    set(payload) != {"id", "ready"}
                ):
                    raise ApiError(
                        403,
                        "Assign accounts can only change house readiness.",
                    )
                house_id = str(payload.get("id") or "").strip()
                with connect_db() as db:
                    with db:
                        if not db.execute(
                            "SELECT 1 FROM provided_houses WHERE id = ?", (house_id,)
                        ).fetchone():
                            raise ApiError(404, "House was not found.")
                        if "notes" in payload:
                            notes = str(payload.get("notes") or "").strip()
                            if len(notes) > 2000:
                                raise ApiError(400, "Notes must be 2,000 characters or fewer.")
                            db.execute(
                                "UPDATE provided_houses SET notes = ? WHERE id = ?",
                                (notes, house_id),
                            )
                        if "ready" in payload:
                            if not isinstance(payload["ready"], bool):
                                raise ApiError(400, "Choose whether the house is ready for start.")
                            if db.execute(
                                "SELECT 1 FROM houses WHERE id = ?", (house_id,)
                            ).fetchone():
                                raise ApiError(409, f"House {house_id} is already activated.")
                            current = db.execute(
                                "SELECT ready_for_start FROM provided_houses WHERE id = ?",
                                (house_id,),
                            ).fetchone()
                            if payload["ready"] and not current["ready_for_start"]:
                                stale_assignment = db.execute(
                                    """
                                    SELECT assignment.assigned_at, history.changed_at
                                    FROM house_role_assignments assignment
                                    JOIN house_status_history history
                                      ON history.house_id = assignment.house_id
                                     AND history.status = 'not_started'
                                    WHERE assignment.house_id = ?
                                      AND assignment.role = 'surveyor'
                                      AND history.id = (
                                          SELECT latest.id
                                          FROM house_status_history latest
                                          WHERE latest.house_id = assignment.house_id
                                            AND latest.status = 'not_started'
                                          ORDER BY latest.changed_at DESC, latest.id DESC
                                          LIMIT 1
                                      )
                                    """,
                                    (house_id,),
                                ).fetchone()
                                if (
                                    stale_assignment
                                    and stale_assignment["assigned_at"]
                                    < stale_assignment["changed_at"]
                                ):
                                    db.execute(
                                        """
                                        DELETE FROM house_role_assignments
                                        WHERE house_id = ? AND role = 'surveyor'
                                        """,
                                        (house_id,),
                                    )
                            if not payload["ready"] and current["ready_for_start"]:
                                _clear_house_survey_records(db, house_id)
                                db.execute(
                                    """
                                    DELETE FROM house_role_assignments
                                    WHERE house_id = ?
                                      AND role IN ('surveyor', 'supervisor')
                                    """,
                                    (house_id,),
                                )
                                db.execute(
                                    "DELETE FROM house_vent_assignments WHERE house_id = ?",
                                    (house_id,),
                                )
                                db.execute(
                                    "DELETE FROM house_plumbing_assignments WHERE house_id = ?",
                                    (house_id,),
                                )
                            db.execute(
                                "UPDATE provided_houses SET ready_for_start = ? WHERE id = ?",
                                (int(payload["ready"]), house_id),
                            )
                            if bool(current["ready_for_start"]) != payload["ready"]:
                                _record_house_status(
                                    db,
                                    house_id,
                                    "ready" if payload["ready"] else "not_started",
                                    user["display_name"],
                                )
                self._send_json(200, {"id": house_id, "saved": True})
            elif path == "/api/stocktakes":
                payload = self._json_body()
                material_id = payload.get("material_id")
                location = str(payload.get("location") or "").strip()
                quantity = parse_quantity(payload.get("quantity"), "Counted quantity", True)
                counted_by = str(payload.get("counted_by") or "").strip()
                notes = str(payload.get("notes") or "").strip()
                if len(notes) > 2000:
                    raise ApiError(400, "Notes must be 2,000 characters or fewer.")
                with connect_db() as db:
                    with db:
                        db.execute("BEGIN IMMEDIATE")
                        if not db.execute(
                            "SELECT 1 FROM materials WHERE id = ?", (material_id,)
                        ).fetchone():
                            raise ApiError(400, "Choose a valid material.")
                        if location == "consumed" or location not in known_locations(db):
                            raise ApiError(400, "Choose a warehouse, team, or house with a recorded balance.")
                        _valid_reference(db, location)
                        if not counted_by:
                            raise ApiError(400, "Record who completed the stocktake.")
                        movement_id = db.execute(
                            "SELECT COALESCE(MAX(id), 0) FROM movements"
                        ).fetchone()[0]
                        db.execute(
                            """
                            INSERT INTO stocktakes
                                (material_id, location, quantity, movement_id, counted_on, counted_by, notes)
                            VALUES (?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                material_id,
                                location,
                                quantity,
                                movement_id,
                                date.today().isoformat(),
                                counted_by,
                                notes,
                            ),
                        )
                self._send_json(201, {"ok": True})
            elif path == "/api/materials":
                payload = self._json_body()
                name = str(payload.get("name") or "").strip()
                code = str(payload.get("code") or "").strip()
                unit = str(payload.get("unit") or "units").strip() or "units"
                opening_qty = parse_quantity(
                    payload.get("opening_qty"), "Opening stock", allow_zero=True
                )
                if not name or len(name) > 200:
                    raise ApiError(400, "Material name is required (maximum 200 characters).")
                if len(code) > 100 or len(unit) > 40:
                    raise ApiError(400, "Material code or unit is too long.")
                with connect_db() as db:
                    with db:
                        cursor = db.execute(
                            "INSERT INTO materials(code, name, unit, opening_qty) VALUES (?, ?, ?, ?)",
                            (code, name, unit, opening_qty),
                        )
                self._send_json(201, {"id": cursor.lastrowid})
            elif path == "/api/calculator/requests":
                user = self._login_required()
                if user is None:
                    return
                payload = self._json_body()
                if not isinstance(payload, dict):
                    raise ApiError(400, "Request data must be a JSON object.")
                house_id = str(payload.get("house_id") or "").strip()
                data = payload.get("data")
                if not isinstance(data, dict):
                    raise ApiError(400, "Material request details are required.")
                encoded = json.dumps(data, ensure_ascii=False)
                if len(encoded) > 100_000:
                    raise ApiError(400, "Material request is too large.")
                notes = str(payload.get("notes") or "").strip()[:2000]
                with connect_db() as db:
                    if not db.execute(
                        "SELECT 1 FROM provided_houses WHERE id = ?", (house_id,)
                    ).fetchone():
                        raise ApiError(404, "Select a house from the provided list first.")
                    if db.execute(
                        "SELECT 1 FROM houses WHERE id = ? AND is_finished = 1",
                        (house_id,),
                    ).fetchone():
                        raise ApiError(409, f"House {house_id} is closed. Reopen it before assigning materials.")
                    cursor = db.execute(
                        """
                        INSERT INTO material_requests(house_id, created_at, created_by, notes, data)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            house_id,
                            datetime.now().isoformat(timespec="seconds"),
                            user["display_name"],
                            notes,
                            encoded,
                        ),
                    )
                self._send_json(201, {"id": cursor.lastrowid})
            elif path == "/api/houses":
                payload = self._json_body()
                if not isinstance(payload, dict):
                    raise ApiError(400, "House data must be a JSON object.")
                house_id = str(payload.get("id") or "").strip()
                if len(house_id) > 120:
                    raise ApiError(400, "House ID must be 120 characters or fewer.")
                supervisor_user_id = None
                if _has_access_profile(user, "assign"):
                    if set(payload) != {"id", "supervisor_user_id"}:
                        raise ApiError(
                            403,
                            "Choose an active Supervisor to activate this house.",
                        )
                    try:
                        if isinstance(payload["supervisor_user_id"], bool):
                            raise ValueError
                        supervisor_user_id = int(payload["supervisor_user_id"])
                    except (TypeError, ValueError, OverflowError) as exc:
                        raise ApiError(400, "Choose a valid Supervisor account.") from exc
                postcode = str(payload.get("postcode") or "").strip()
                owner_name = str(payload.get("owner_name") or "").strip()
                employee_name = str(payload.get("employee_name") or "").strip()
                if len(postcode) > 80 or len(owner_name) > 160 or len(employee_name) > 160:
                    raise ApiError(400, "House details exceed the allowed length.")
                with connect_db() as db:
                    with db:
                        _require_ready_to_activate(db, house_id)
                        if _has_access_profile(user, "assign"):
                            ready_house = db.execute(
                                """
                                SELECT 1 FROM provided_houses p
                                LEFT JOIN houses h ON h.id = p.id
                                WHERE p.id = ? AND p.ready_for_start = 1
                                  AND (h.id IS NULL OR h.is_finished = 0)
                                """,
                                (house_id,),
                            ).fetchone()
                            if not ready_house:
                                raise ApiError(
                                    409,
                                    "Assign users can activate only houses marked Ready for start.",
                                )
                            surveyor = db.execute(
                                """
                                SELECT assigned_user.id
                                FROM house_role_assignments assignment
                                JOIN app_users assigned_user ON assigned_user.id = assignment.user_id
                                WHERE assignment.house_id = ? AND assignment.role = 'surveyor'
                                  AND assigned_user.is_active = 1
                                  AND EXISTS (
                                      SELECT 1 FROM app_role_permissions access
                                      WHERE access.role_key = assigned_user.role
                                        AND access.permission_key = 'access.surveyor'
                                        AND access.is_allowed = 1
                                  )
                                """,
                                (house_id,),
                            ).fetchone()
                            if not surveyor:
                                raise ApiError(
                                    409,
                                    "Assign an active Surveyor before activating this house.",
                                )
                            supervisor = db.execute(
                                """
                                SELECT id, display_name FROM app_users
                                WHERE id = ? AND is_active = 1
                                  AND EXISTS (
                                      SELECT 1 FROM app_role_permissions access
                                      WHERE access.role_key = app_users.role
                                        AND access.permission_key = 'access.supervisor'
                                        AND access.is_allowed = 1
                                  )
                                """,
                                (supervisor_user_id,),
                            ).fetchone()
                            if not supervisor:
                                raise ApiError(400, "Choose an active Supervisor account.")
                            survey_row = db.execute(
                                "SELECT data FROM house_surveys WHERE house_id = ?",
                                (house_id,),
                            ).fetchone()
                            if not survey_row:
                                raise ApiError(
                                    409,
                                    "Surveyor review is required before activation. Record the house survey first.",
                                )
                            try:
                                survey_data = json.loads(survey_row["data"])
                                perimeter = float(survey_data.get("perimeter_m"))
                            except (
                                AttributeError,
                                TypeError,
                                ValueError,
                                OverflowError,
                                json.JSONDecodeError,
                            ):
                                raise ApiError(
                                    409,
                                    "A valid surveyed perimeter is required before activation.",
                                )
                            if (
                                not isinstance(survey_data, dict)
                                or not math.isfinite(perimeter)
                                or not (0 < perimeter <= 10000)
                            ):
                                raise ApiError(
                                    409,
                                    "A valid surveyed perimeter is required before activation.",
                                )
                            employee_name = supervisor["display_name"]
                            payload["perimeter_m"] = perimeter
                            db.execute(
                                """
                                INSERT INTO house_role_assignments
                                    (house_id, role, user_id, assigned_by, assigned_at)
                                VALUES (?, 'supervisor', ?, ?, ?)
                                ON CONFLICT(house_id, role) DO UPDATE SET
                                    user_id = excluded.user_id,
                                    assigned_by = excluded.assigned_by,
                                    assigned_at = excluded.assigned_at
                                """,
                                (
                                    house_id,
                                    supervisor["id"],
                                    user["display_name"],
                                    datetime.now().isoformat(timespec="seconds"),
                                ),
                            )
                        if not employee_name:
                            raise ApiError(
                                400,
                                "Assign an employee before opening the calculator.",
                            )
                        try:
                            perimeter = float(payload.get("perimeter_m"))
                        except (TypeError, ValueError, OverflowError):
                            raise ApiError(
                                400,
                                "Enter a surveyed perimeter greater than 0 and no more than 10,000 metres.",
                            )
                        if not math.isfinite(perimeter) or not (0 < perimeter <= 10000):
                            raise ApiError(
                                400,
                                "Enter a surveyed perimeter greater than 0 and no more than 10,000 metres.",
                            )
                        survey_row = db.execute(
                            "SELECT data FROM house_surveys WHERE house_id = ?",
                            (house_id,),
                        ).fetchone()
                        try:
                            survey_data = json.loads(survey_row["data"])
                        except (TypeError, json.JSONDecodeError):
                            raise ApiError(500, "Stored house survey data is invalid.")
                        if not isinstance(survey_data, dict):
                            raise ApiError(500, "Stored house survey data is invalid.")
                        if not _has_access_profile(user, "assign"):
                            survey_data["perimeter_m"] = perimeter
                            db.execute(
                                "UPDATE house_surveys SET data = ?, updated_by = ?, "
                                "updated_at = ? WHERE house_id = ?",
                                (
                                    json.dumps(survey_data, ensure_ascii=False),
                                    user["display_name"],
                                    datetime.now().isoformat(timespec="seconds"),
                                    house_id,
                                ),
                            )
                        existing_house = db.execute(
                            """
                            SELECT COALESCE(p.ready_for_start, 0) AS ready_for_start
                            FROM houses h
                            LEFT JOIN provided_houses p ON p.id = h.id
                            WHERE h.id = ?
                            """,
                            (house_id,),
                        ).fetchone()
                        was_active = (
                            existing_house is not None
                            and not existing_house["ready_for_start"]
                        )
                        provided_house = db.execute(
                            "SELECT postcode, owner_name FROM provided_houses WHERE id = ?",
                            (house_id,),
                        ).fetchone()
                        if provided_house:
                            postcode = provided_house["postcode"] or postcode
                            owner_name = provided_house["owner_name"] or owner_name
                        _upsert_house(
                            db,
                            house_id,
                            postcode,
                            owner_name,
                            employee_name,
                        )
                        if db.execute(
                            """
                            SELECT 1 FROM house_plumbing_assignments
                            WHERE house_id = ?
                            """,
                            (house_id,),
                        ).fetchone():
                            ewi_supervisor = db.execute(
                                """
                                SELECT users.id
                                FROM app_users users
                                WHERE users.is_active = 1
                                  AND users.display_name = ? COLLATE NOCASE
                                  AND users.supervisor_specialty =
                                      'External Wall Insulation' COLLATE NOCASE
                                  AND EXISTS (
                                      SELECT 1 FROM app_role_permissions access
                                      WHERE access.role_key = users.role
                                        AND access.permission_key = 'access.supervisor'
                                        AND access.is_allowed = 1
                                  )
                                """,
                                (employee_name,),
                            ).fetchone()
                            if ewi_supervisor:
                                db.execute(
                                    """
                                    INSERT INTO house_role_assignments
                                        (house_id, role, user_id, assigned_by, assigned_at)
                                    VALUES (?, 'supervisor', ?, ?, ?)
                                    ON CONFLICT(house_id, role) DO UPDATE SET
                                        user_id = excluded.user_id,
                                        assigned_by = excluded.assigned_by,
                                        assigned_at = excluded.assigned_at
                                    """,
                                    (
                                        house_id,
                                        ewi_supervisor["id"],
                                        user["display_name"],
                                        datetime.now().isoformat(timespec="seconds"),
                                    ),
                                )
                        if not was_active:
                            _record_house_status(
                                db, house_id, "active", user["display_name"]
                            )
                self._send_json(201, {"id": house_id})
            elif path == "/api/houses/return-ready":
                payload = self._json_body()
                if not isinstance(payload, dict) or set(payload) != {"id"}:
                    raise ApiError(400, "A house ID is required.")
                house_id = payload["id"]
                if not isinstance(house_id, str):
                    raise ApiError(400, "Enter a valid house ID.")
                house_id = house_id.strip()
                if not house_id or len(house_id) > 120:
                    raise ApiError(400, "Enter a valid house ID.")
                with connect_db() as db:
                    with db:
                        db.execute("BEGIN IMMEDIATE")
                        house = db.execute(
                            """
                            SELECT p.ready_for_start, h.is_finished
                            FROM provided_houses p
                            JOIN houses h ON h.id = p.id
                            WHERE p.id = ?
                            """,
                            (house_id,),
                        ).fetchone()
                        if house is None:
                            raise ApiError(404, "Active house was not found.")
                        if house["is_finished"]:
                            raise ApiError(
                                409, "A closed house cannot be returned to Ready for start."
                            )
                        if house["ready_for_start"]:
                            raise ApiError(
                                409, "This house is already Ready for start."
                            )
                        db.execute(
                            "UPDATE provided_houses SET ready_for_start = 1 WHERE id = ?",
                            (house_id,),
                        )
                        db.execute(
                            "UPDATE houses SET status_changed_at = ? WHERE id = ?",
                            (datetime.now().timestamp(), house_id),
                        )
                        _record_house_status(
                            db, house_id, "ready", user["display_name"]
                        )
                self._send_json(200, {"id": house_id, "status": "ready"})
            elif path == "/api/houses/status":
                payload = self._json_body()
                if not isinstance(payload, dict) or not isinstance(payload.get("finished"), bool):
                    raise ApiError(400, "Choose whether the house is finished.")
                house_id = str(payload.get("id") or "").strip()
                if not house_id:
                    raise ApiError(400, "House ID is required.")
                closeout_count = 0
                restored_count = 0
                with connect_db() as db:
                    with db:
                        db.execute("BEGIN IMMEDIATE")
                        house = db.execute(
                            "SELECT id, employee_name, is_finished FROM houses WHERE id = ?",
                            (house_id,),
                        ).fetchone()
                        if house is None:
                            raise ApiError(404, "House was not found.")
                        if not payload["finished"]:
                            restored_count = _undo_auto_closeouts(db, house_id)
                        if payload["finished"]:
                            material_rows = db.execute(
                                "SELECT id, name FROM materials ORDER BY name COLLATE NOCASE"
                            ).fetchall()
                            house_location = f"house:{house_id}"
                            balances = [
                                (material, balances_for(db, material["id"]).get(house_location, 0))
                                for material in material_rows
                            ]
                            negative = [
                                (material["name"], balance)
                                for material, balance in balances
                                if balance < -1e-9
                            ]
                            if negative:
                                detail = ", ".join(
                                    f"{name}: {balance:g}" for name, balance in negative
                                )
                                raise ApiError(
                                    409,
                                    f"Cannot finish this house while material balances are negative. Reconcile first: {detail}.",
                                )
                            for material, balance in balances:
                                if balance <= 1e-9:
                                    continue
                                _record_movement(
                                    db,
                                    {
                                        "kind": "use",
                                        "date": date.today().isoformat(),
                                        "material_id": material["id"],
                                        "qty": balance,
                                        "house_id": house_id,
                                        "person": user["display_name"],
                                        "team": "",
                                        "notes": CLOSEOUT_NOTE,
                                    },
                                )
                                closeout_count += 1
                        cursor = db.execute(
                            """
                            UPDATE houses
                            SET is_finished = ?
                            WHERE id = ?
                            """,
                            (
                                int(payload["finished"]),
                                house_id,
                            ),
                        )
                        if bool(house["is_finished"]) != payload["finished"]:
                            _record_house_status(
                                db,
                                house_id,
                                "closed" if payload["finished"] else "active",
                                user["display_name"],
                            )
                self._send_json(
                    200,
                    {
                        "id": house_id,
                        "finished": payload["finished"],
                        "materials_closed_out": closeout_count,
                        "materials_restored": restored_count,
                    },
                )
            elif path == "/api/houses/import":
                user = self._login_required()
                if user is None:
                    return
                if not self._admin_required(user):
                    return
                content_type = self.headers.get("Content-Type", "")
                if not content_type.startswith("multipart/form-data;"):
                    raise ApiError(400, "Upload the approved house-location CSV file.")
                body = self._body()
                match = re.search(r'boundary="?([^";]+)"?', content_type)
                if not match:
                    raise ApiError(400, "Invalid house-location file upload.")
                boundary = match.group(1).encode("ascii")
                file_data = None
                for part in body.split(b"--" + boundary):
                    if b"filename=" not in part or b"\r\n\r\n" not in part:
                        continue
                    headers, candidate = part.split(b"\r\n\r\n", 1)
                    filename = re.search(rb'filename="([^"]*)"', headers)
                    if filename and not filename.group(1).lower().endswith(b".csv"):
                        raise ApiError(400, "House locations must be uploaded as a CSV file.")
                    file_data = candidate.rsplit(b"\r\n", 1)[0]
                    break
                if not file_data:
                    raise ApiError(400, "Choose a house-location CSV file to import.")
                with connect_db() as db:
                    with db:
                        result = _import_house_locations_csv(db, file_data)
                self._send_json(201, result)
            elif path == "/api/import":
                content_type = self.headers.get("Content-Type", "")
                if not content_type.startswith("multipart/form-data;"):
                    raise ApiError(400, "Upload an Excel workbook using the file picker.")
                body = self._body()
                match = re.search(r'boundary="?([^";]+)"?', content_type)
                if not match:
                    raise ApiError(400, "Invalid file upload.")
                boundary = match.group(1).encode("ascii")
                parts = body.split(b"--" + boundary)
                file_data = None
                for part in parts:
                    if b"filename=" in part and b"\r\n\r\n" in part:
                        file_data = part.split(b"\r\n\r\n", 1)[1].rsplit(b"\r\n", 1)[0]
                        break
                if not file_data:
                    raise ApiError(400, "Choose an Excel workbook to import.")
                temporary_file = ROOT / "instance" / "workbook-upload.xlsx"
                temporary_file.parent.mkdir(parents=True, exist_ok=True)
                try:
                    temporary_file.write_bytes(file_data)
                    with connect_db() as db:
                        with db:
                            result = _import_workbook(db, temporary_file)
                    self._send_json(201, result)
                finally:
                    temporary_file.unlink(missing_ok=True)
            else:
                self._send_json(404, {"error": "Page not found."})
        except ApiError as exc:
            self._send_json(exc.status, {"error": str(exc)})
        except sqlite3.IntegrityError as exc:
            error_text = str(exc)
            if "materials.name" in error_text:
                message = "A material with that name already exists."
            elif "app_users.username" in error_text:
                message = "That username is already in use."
            else:
                message = "This record conflicts with existing tracker data."
            self._send_json(409, {"error": message})
        except (OSError, sqlite3.Error) as exc:
            self._send_json(500, {"error": f"Could not save the record: {exc}"})


class FastAPIRequestContext(LegacyRouteHandlers):
    def __init__(self, method, path, headers, body, client_address):
        self.command = method
        self.path = path
        self.headers = headers
        self.rfile = BytesIO(body)
        self.wfile = BytesIO()
        self.client_address = client_address
        self.status = 200
        self.response_headers = []

    def send_response(self, status, message=None):
        self.status = status

    def send_header(self, name, value):
        self.response_headers.append((name, value))

    def end_headers(self):
        pass

    def address_string(self):
        return self.client_address[0] if self.client_address else ""

    def response(self):
        response = Response(
            content=self.wfile.getvalue(),
            status_code=self.status,
        )
        response.raw_headers = [
            (name.encode("latin-1"), value.encode("latin-1"))
            for name, value in self.response_headers
        ]
        return response


@asynccontextmanager
async def _app_lifespan(application):
    if getattr(application.state, "initialized_db_path", None) != DB_PATH:
        await run_in_threadpool(initialize_db)
        application.state.initialized_db_path = DB_PATH
    yield


app = FastAPI(
    title="Material Tracker API",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=_app_lifespan,
)


def _run_legacy_request(method, path, headers, body, client_address):
    context = FastAPIRequestContext(
        method,
        path,
        headers,
        body,
        client_address,
    )
    try:
        if method == "GET":
            context.do_GET()
        else:
            context.do_POST()
    except ApiError as exc:
        context._send_json(exc.status, {"error": str(exc)})
    return context.response()


async def _dispatch_fastapi_request(request):
    body = bytearray()
    if request.method == "POST":
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 10_000_000:
                context = FastAPIRequestContext(
                    request.method,
                    request.url.path,
                    request.headers,
                    b"",
                    request.client,
                )
                context._send_json(400, {"error": "Request body is empty or too large."})
                return context.response()

    raw_path = request.scope.get("raw_path", request.url.path.encode("ascii"))
    path = raw_path.decode("ascii")
    query = request.scope.get("query_string", b"")
    if query:
        path += "?" + query.decode("ascii")
    client_address = (
        (request.client.host, request.client.port)
        if request.client
        else None
    )
    return await run_in_threadpool(
        _run_legacy_request,
        request.method,
        path,
        request.headers,
        bytes(body),
        client_address,
    )


@app.api_route("/", methods=["GET", "POST"])
@app.api_route("/{path:path}", methods=["GET", "POST"])
async def dispatch_request(request: FastAPIRequest):
    return await _dispatch_fastapi_request(request)


def run():
    initialize_db()
    app.state.initialized_db_path = DB_PATH
    if bool(TLS_CERT) != bool(TLS_KEY):
        raise RuntimeError("Set both EWI_TLS_CERT and EWI_TLS_KEY to enable HTTPS.")
    if HOST not in {"127.0.0.1", "localhost", "::1"} and not COOKIE_SECURE:
        raise RuntimeError(
            "Binding to a network interface requires HTTPS; configure EWI_TLS_CERT and EWI_TLS_KEY."
        )
    with connect_db() as db:
        if HOST not in {"127.0.0.1", "localhost", "::1"} and not _user_count(db):
            raise RuntimeError(
                "Create the first administrator on localhost before enabling network access."
            )
    scheme = "https" if COOKIE_SECURE else "http"
    print(f"EWI Tracker running at {scheme}://{HOST}:{PORT}")
    try:
        uvicorn.run(
            app,
            host=HOST,
            port=PORT,
            ssl_certfile=TLS_CERT or None,
            ssl_keyfile=TLS_KEY or None,
            ssl_version=ssl.PROTOCOL_TLS_SERVER,
        )
    except KeyboardInterrupt:
        print("\nStopping EWI Tracker.")


if __name__ == "__main__":
    run()
