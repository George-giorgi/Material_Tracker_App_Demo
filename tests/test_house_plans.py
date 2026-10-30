import http.client
import json
import struct
import tempfile
import threading
import unittest
from io import BytesIO
from pathlib import Path
from PIL import Image, ImageDraw
import pymupdf as fitz

import house_plans
import house_model
import server

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None


class HousePlanAnalysisTests(unittest.TestCase):
    def make_vector_pdf(self):
        document = fitz.open()
        page = document.new_page(width=600, height=800)
        page.draw_rect(fitz.Rect(70, 90, 530, 720), width=3)
        page.draw_line(fitz.Point(300, 90), fitz.Point(300, 720), width=2)
        page.insert_text((100, 140), "Kitchen")
        page.insert_text((100, 170), "3.60 m x 4.20 m")
        result = document.tobytes()
        document.close()
        return result

    def make_scanned_pdf(self):
        image = Image.new("RGB", (600, 800), "white")
        drawing = ImageDraw.Draw(image)
        drawing.rectangle((70, 90, 530, 720), outline="black", width=5)
        drawing.line((300, 90, 300, 720), fill="black", width=4)
        drawing.text((110, 150), "KITCHEN 3.6 m x 4.2 m", fill="black")
        png = BytesIO()
        image.save(png, format="PNG")

        document = fitz.open()
        page = document.new_page(width=600, height=800)
        page.insert_image(page.rect, stream=png.getvalue())
        result = document.tobytes()
        document.close()
        return result

    def test_vector_pdf_reports_text_dimensions_and_unverified_status(self):
        report = house_plans.analyze_pdf(self.make_vector_pdf())

        self.assertEqual(report["content_type"], "vector")
        self.assertEqual(report["page_count"], 1)
        self.assertGreater(report["pages"][0]["vector_objects"], 0)
        self.assertIn("Kitchen", report["pages"][0]["labels"])
        self.assertTrue(report["dimensions"])
        self.assertTrue(
            all(item["status"] == "unverified" for item in report["dimensions"])
        )

    def test_scanned_and_mixed_pdfs_are_classified(self):
        scanned = house_plans.analyze_pdf(self.make_scanned_pdf())
        self.assertEqual(scanned["content_type"], "scanned")
        self.assertTrue(scanned["pages"][0]["scanned_image_likely"])
        if not scanned["ocr_available"]:
            self.assertEqual(
                scanned["pages"][0]["ocr_status"],
                "unavailable-install-tesseract",
            )

        mixed_pdf = fitz.open()
        mixed_pdf.insert_pdf(fitz.open(stream=self.make_vector_pdf(), filetype="pdf"))
        mixed_pdf.insert_pdf(fitz.open(stream=self.make_scanned_pdf(), filetype="pdf"))
        mixed = house_plans.analyze_pdf(mixed_pdf.tobytes())
        mixed_pdf.close()
        self.assertEqual(mixed["content_type"], "mixed")

    def test_invalid_and_oversized_page_count_pdfs_are_rejected(self):
        with self.assertRaises(house_plans.PdfAnalysisError):
            house_plans.analyze_pdf(b"%PDF-not-a-valid-document")

        document = fitz.open()
        for _ in range(house_plans.MAX_PAGES + 1):
            document.new_page()
        data = document.tobytes()
        document.close()
        with self.assertRaises(house_plans.PdfAnalysisError):
            house_plans.analyze_pdf(data)

    def test_scanned_and_vector_pdf_pages_render_for_source_tracing(self):
        pdf = self.make_vector_pdf()
        preview = house_plans.render_page_png(pdf, 1)
        self.assertTrue(preview.startswith(b"\x89PNG\r\n\x1a\n"))
        with self.assertRaises(house_plans.PdfAnalysisError):
            house_plans.render_page_png(pdf, 2)

    def test_exterior_outline_suggestion_is_normalized_and_unconfirmed(self):
        pdf = self.make_vector_pdf()
        suggestion = house_plans.suggest_exterior_outline(pdf, 1)

        self.assertGreaterEqual(len(suggestion["points"]), 4)
        self.assertTrue(
            all(
                0 <= point[axis] <= 1
                for point in suggestion["points"]
                for axis in ("x", "y")
            )
        )
        self.assertGreater(suggestion["width"], 0)
        scanned_suggestion = house_plans.suggest_exterior_outline(
            self.make_scanned_pdf(), 1
        )
        self.assertGreaterEqual(len(scanned_suggestion["points"]), 4)
        with self.assertRaises(house_plans.PdfAnalysisError):
            house_plans.suggest_exterior_outline(pdf, 2)


class HousePlanGlbTests(unittest.TestCase):
    def test_irregular_confirmed_walls_export_to_a_self_contained_glb(self):
        points = [(0, 0), (6, 0), (6, 3), (4, 3), (4, 5), (0, 5)]
        walls = [
            {
                "id": f"wall-{index}",
                "label": f"Wall {index + 1}",
                "x1": start[0],
                "y1": start[1],
                "x2": end[0],
                "y2": end[1],
                "height": 2.7,
                "thickness": 0.3,
                "exterior": True,
                "page": 1,
            }
            for index, (start, end) in enumerate(
                zip(points, points[1:] + points[:1])
            )
        ]
        model, floor_generated = house_model.create_house_glb(walls)

        magic, version, total_length = struct.unpack("<4sII", model[:12])
        json_length, json_type = struct.unpack("<I4s", model[12:20])
        gltf = json.loads(model[20 : 20 + json_length])
        self.assertEqual(magic, b"glTF")
        self.assertEqual(version, 2)
        self.assertEqual(total_length, len(model))
        self.assertEqual(json_type, b"JSON")
        self.assertTrue(floor_generated)
        self.assertEqual(len(gltf["nodes"]), len(walls) + 1)
        self.assertEqual(gltf["scenes"][0]["extras"]["roofAvailable"], False)
        self.assertIn("Illustrative oak floor", [m["name"] for m in gltf["materials"]])
        self.assertIn(b"\x89PNG\r\n\x1a\n", model)
        self.assertTrue(
            all(node.get("extras", {}).get("confirmedGeometry") for node in gltf["nodes"][:-1])
        )

    def test_open_exterior_trace_exports_walls_without_inventing_a_floor(self):
        wall = {
            "id": "north",
            "label": "North",
            "x1": 0,
            "y1": 0,
            "x2": 5,
            "y2": 0,
            "height": 2.7,
            "thickness": 0.3,
            "exterior": True,
        }
        model, floor_generated = house_model.create_house_glb([wall])
        self.assertTrue(model.startswith(b"glTF"))
        self.assertFalse(floor_generated)


class HousePlanApiTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = server.DB_PATH
        server.DB_PATH = Path(self.temp_dir.name) / "plans.sqlite3"
        server.initialize_db()
        self.httpd = server.ThreadingHTTPServer(
            ("127.0.0.1", 0), server.RequestHandler
        )
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        _, _, self.admin_cookie = self.request(
            "POST",
            "/api/auth/setup",
            json.dumps(
                {"username": "plan-admin", "display_name": "Plan Admin", "pin": "123456"}
            ).encode(),
            "application/json",
        )
        with server.connect_db() as db:
            surveyor_id = server._insert_user(
                db, "plan-surveyor", "Plan Surveyor", "654321", "surveyor"
            )
            surveyor_token, _, _ = server._new_session(db, surveyor_id)
        self.surveyor_cookie = f"ewi_session={surveyor_token}"
        with server.connect_db() as db:
            employee_id = server._insert_user(
                db, "plan-employee", "Plan Employee", "135790", "employee"
            )
            employee_token, _, _ = server._new_session(db, employee_id)
        self.employee_cookie = f"ewi_session={employee_token}"

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)
        server.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    def request(self, method, path, body=None, content_type=None, cookie=""):
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.httpd.server_address[1], timeout=10
        )
        headers = {}
        if content_type:
            headers["Content-Type"] = content_type
        if cookie:
            headers["Cookie"] = cookie
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        content = response.read()
        status = response.status
        response_type = response.getheader("Content-Type", "")
        set_cookie = response.getheader("Set-Cookie", "").split(";", 1)[0]
        connection.close()
        payload = json.loads(content) if "application/json" in response_type else content
        return status, payload, set_cookie

    def upload(self, cookie, filename, content):
        boundary = "----HousePlanUploadBoundary"
        body = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
            "Content-Type: application/pdf\r\n\r\n"
        ).encode() + content + f"\r\n--{boundary}--\r\n".encode()
        return self.request(
            "POST",
            "/api/house-plans",
            body,
            f"multipart/form-data; boundary={boundary}",
            cookie,
        )

    def test_upload_list_and_original_pdf_access_are_isolated_by_uploader(self):
        document = fitz.open()
        page = document.new_page()
        page.draw_rect(fitz.Rect(80, 100, 520, 700), width=3)
        page.insert_text((72, 72), "Bedroom 3.2 m")
        pdf = document.tobytes()
        document.close()

        status, uploaded, _ = self.upload(self.admin_cookie, "plan.pdf", pdf)
        self.assertEqual(status, 201)
        self.assertEqual(uploaded["analysis"]["content_type"], "vector")

        status, admin_list, _ = self.request(
            "GET", "/api/house-plans", cookie=self.admin_cookie
        )
        self.assertEqual(status, 200)
        self.assertEqual(len(admin_list["plans"]), 1)

        status, surveyor_list, _ = self.request(
            "GET", "/api/house-plans", cookie=self.surveyor_cookie
        )
        self.assertEqual(status, 200)
        self.assertEqual(surveyor_list["plans"], [])

        status, _, _ = self.request(
            "GET",
            f"/api/house-plans/{uploaded['id']}/file",
            cookie=self.surveyor_cookie,
        )
        self.assertEqual(status, 404)

        status, original, _ = self.request(
            "GET",
            f"/api/house-plans/{uploaded['id']}/file",
            cookie=self.admin_cookie,
        )
        self.assertEqual(status, 200)
        self.assertEqual(original, pdf)

        status, preview, _ = self.request(
            "GET",
            f"/api/house-plans/{uploaded['id']}/page/1.png",
            cookie=self.admin_cookie,
        )
        self.assertEqual(status, 200)
        self.assertTrue(preview.startswith(b"\x89PNG\r\n\x1a\n"))

        status, suggestion, _ = self.request(
            "GET",
            f"/api/house-plans/{uploaded['id']}/suggestions/exterior/1",
            cookie=self.admin_cookie,
        )
        self.assertEqual(status, 200)
        self.assertEqual(suggestion["status"], "unconfirmed")
        self.assertGreaterEqual(len(suggestion["points"]), 4)
        status, hidden_suggestion, _ = self.request(
            "GET",
            f"/api/house-plans/{uploaded['id']}/suggestions/exterior/1",
            cookie=self.surveyor_cookie,
        )
        self.assertEqual(status, 403)

        status, missing, _ = self.request(
            "GET",
            f"/api/house-plans/{uploaded['id']}/model.glb",
            cookie=self.admin_cookie,
        )
        self.assertEqual(status, 409)
        self.assertIn("Trace and save", missing["error"])

        status, hidden_preview, _ = self.request(
            "GET",
            f"/api/house-plans/{uploaded['id']}/page/1.png",
            cookie=self.surveyor_cookie,
        )
        self.assertEqual(status, 404)

    def test_upload_is_restricted_and_invalid_pdfs_are_rejected(self):
        status, _, _ = self.request("GET", "/api/house-plans")
        self.assertEqual(status, 401)

        status, _, _ = self.request("GET", "/house-plans")
        self.assertEqual(status, 401)

        status, _, _ = self.request(
            "GET", "/api/house-plans", cookie=self.employee_cookie
        )
        self.assertEqual(status, 403)
        status, _, _ = self.request(
            "GET", "/house-plans", cookie=self.employee_cookie
        )
        self.assertEqual(status, 403)

        status, page, _ = self.request(
            "GET", "/house-plans", cookie=self.surveyor_cookie
        )
        self.assertEqual(status, 200)
        self.assertIn(b"Floor-plan to 3D model", page)

        status, result, _ = self.upload(
            self.admin_cookie, "not-a-plan.pdf", b"%PDF-not-a-valid-document"
        )
        self.assertEqual(status, 400)
        self.assertIn("could not be opened", result["error"])

    def test_confirmed_wall_geometry_round_trips_and_rejects_invalid_values(self):
        document = fitz.open()
        document.new_page()
        pdf = document.tobytes()
        document.close()
        status, uploaded, _ = self.upload(
            self.surveyor_cookie, "measured-plan.pdf", pdf
        )
        self.assertEqual(status, 201)
        document_id = uploaded["id"]
        endpoint = f"/api/house-plans/{document_id}/geometry"
        walls = [
            {
                "id": "north-wall",
                "label": "North exterior",
                "x1": 0,
                "y1": 0,
                "x2": 5,
                "y2": 0,
                "height": 2.7,
                "thickness": 0.3,
                "exterior": True,
                "page": 1,
                "color": "#c05a40",
            },
            {
                "id": "angled-partition",
                "label": "Angled partition",
                "x1": 5,
                "y1": 0,
                "x2": 7,
                "y2": 2,
                "height": 2.5,
                "thickness": 0.12,
                "exterior": False,
                "page": 1,
            },
        ]
        annotations = [
            {"id": "vent-note", "label": "Vent", "x": 0.25, "y": 0.4, "page": 1}
        ]
        model_annotations = [
            {
                "id": "vent-3d",
                "title": "Vent",
                "note": "Check extractor",
                "x": 1.25,
                "y": 2.4,
                "z": -0.15,
            }
        ]
        status, saved, _ = self.request(
            "POST",
            endpoint,
            json.dumps(
                {
                    "walls": walls,
                    "annotations": annotations,
                    "model_annotations": model_annotations,
                    "calibration": {"1": 0.1},
                }
            ).encode(),
            "application/json",
            self.surveyor_cookie,
        )
        self.assertEqual(status, 200)
        self.assertEqual(saved["geometry"]["walls"], walls)
        self.assertEqual(saved["geometry"]["annotations"], annotations)
        self.assertEqual(saved["geometry"]["model_annotations"], model_annotations)
        self.assertEqual(saved["geometry"]["calibration"], {"1": 0.1})

        status, persisted, _ = self.request(
            "GET", endpoint, cookie=self.surveyor_cookie
        )
        self.assertEqual(status, 200)
        self.assertEqual(persisted["geometry"]["walls"], walls)
        self.assertEqual(persisted["geometry"]["annotations"], annotations)
        self.assertEqual(
            persisted["geometry"]["model_annotations"], model_annotations
        )
        self.assertEqual(persisted["geometry"]["calibration"], {"1": 0.1})

        status, model, _ = self.request(
            "GET",
            f"/api/house-plans/{document_id}/model.glb",
            cookie=self.surveyor_cookie,
        )
        self.assertEqual(status, 200)
        self.assertTrue(model.startswith(b"glTF"))
        json_length, _ = struct.unpack("<I4s", model[12:20])
        gltf = json.loads(model[20 : 20 + json_length])
        self.assertEqual(len(gltf["nodes"]), len(walls))
        angled_wall = next(
            node for node in gltf["nodes"] if node["name"] == "Angled partition"
        )
        self.assertEqual(angled_wall["extras"]["midpoint"], [6, 0, 1])
        position_accessors = [
            gltf["accessors"][primitive["attributes"]["POSITION"]]
            for primitive in gltf["meshes"][angled_wall["mesh"]]["primitives"]
        ]
        self.assertGreater(
            max(accessor["max"][2] for accessor in position_accessors), 1.9
        )
        colored_wall = next(
            node for node in gltf["nodes"] if node["name"] == "North exterior"
        )
        self.assertEqual(colored_wall["extras"]["color"], "#c05a40")
        self.assertIn(
            "Wall marking #C05A40",
            [material["name"] for material in gltf["materials"]],
        )

        invalid_walls = [dict(walls[0], height=0)]
        status, invalid, _ = self.request(
            "POST",
            endpoint,
            json.dumps({"walls": invalid_walls}).encode(),
            "application/json",
            self.surveyor_cookie,
        )
        self.assertEqual(status, 400)
        self.assertIn("Walls must be", invalid["error"])

        invalid_color = [dict(walls[0], color="red")]
        status, invalid, _ = self.request(
            "POST",
            endpoint,
            json.dumps({"walls": invalid_color}).encode(),
            "application/json",
            self.surveyor_cookie,
        )
        self.assertEqual(status, 400)
        self.assertIn("six-digit hex color", invalid["error"])

        invalid_annotation = [
            {"id": "bad-note", "label": "Vent", "x": 1.1, "y": 0.4, "page": 1}
        ]
        status, invalid, _ = self.request(
            "POST",
            endpoint,
            json.dumps({"walls": walls, "annotations": invalid_annotation}).encode(),
            "application/json",
            self.surveyor_cookie,
        )
        self.assertEqual(status, 400)
        self.assertIn("normalized", invalid["error"])

        invalid_model_annotation = [
            {
                "id": "invalid-note",
                "title": "Vent",
                "note": "",
                "x": 0,
                "y": 0,
                "z": 10001,
            }
        ]
        status, invalid, _ = self.request(
            "POST",
            endpoint,
            json.dumps(
                {"walls": walls, "model_annotations": invalid_model_annotation}
            ).encode(),
            "application/json",
            self.surveyor_cookie,
        )
        self.assertEqual(status, 400)
        self.assertIn("finite metres", invalid["error"])

        status, protected, _ = self.request(
            "GET", endpoint, cookie=self.employee_cookie
        )
        self.assertEqual(status, 403)

        status, hidden, _ = self.request(
            "GET",
            f"/api/house-plans/{document_id}/geometry",
            cookie=self.admin_cookie,
        )
        self.assertEqual(status, 200)
        self.assertEqual(hidden["geometry"]["walls"], walls)


@unittest.skipUnless(sync_playwright, "Playwright is required for 3D viewer tests.")
class HousePlanViewerTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = server.DB_PATH
        server.DB_PATH = Path(self.temp_dir.name) / "viewer.sqlite3"
        server.initialize_db()
        self.httpd = server.ThreadingHTTPServer(
            ("127.0.0.1", 0), server.RequestHandler
        )
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.playwright = sync_playwright().start()
        self.browser = self.playwright.chromium.launch()
        self.page = self.browser.new_page()
        self.page.route(
            "**/api/weather",
            lambda route: route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({"areas": [], "issued": None}),
            ),
        )
        self.errors = []
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))
        self.base_url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.page.goto(self.base_url)
        self.page.locator("#auth-display-name").fill("3D Test Admin")
        self.page.locator("#auth-username").fill("3d-test-admin")
        self.page.locator("#auth-pin").fill("123456")
        self.page.locator("#auth-pin-confirm").fill("123456")
        self.page.locator("#auth-submit").click()
        self.page.locator(".app-shell:not(.hidden)").wait_for()

    def tearDown(self):
        self.browser.close()
        self.playwright.stop()
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)
        server.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    def test_pdf_traces_generate_a_real_rotatable_glb_and_persist(self):
        document = fitz.open()
        page = document.new_page(width=600, height=800)
        page.draw_rect(fitz.Rect(80, 100, 520, 700), width=3)
        pdf = document.tobytes()
        document.close()

        self.page.goto(f"{self.base_url}/house-plans")
        self.page.locator("#plan-file").set_input_files(
            {
                "name": "measured-plan.pdf",
                "mimeType": "application/pdf",
                "buffer": pdf,
            }
        )
        self.page.locator("#upload-button").click()
        workspace = self.page.locator(".model-workspace")
        workspace.wait_for()
        canvas = workspace.locator(".trace-overlay")
        image = workspace.locator(".plan-reference")
        image.wait_for()
        self.page.wait_for_function(
            "() => document.querySelector('.plan-reference')?.naturalWidth > 0"
        )
        self.assertAlmostEqual(
            canvas.evaluate("(overlay) => overlay.getBoundingClientRect().height"),
            image.evaluate("(page) => page.getBoundingClientRect().height"),
            delta=1,
        )
        fit_bounds = canvas.bounding_box()
        workspace.locator(".plan-tracer").evaluate("(frame) => { frame.scrollTop = 0; frame.scrollLeft = 0; }")
        zoom_width = workspace.locator(".plan-image-layer").evaluate(
            "(layer) => layer.getBoundingClientRect().width"
        )
        workspace.get_by_role("button", name="Zoom in on source plan").click()
        self.assertGreater(
            workspace.locator(".plan-image-layer").evaluate(
                "(layer) => layer.getBoundingClientRect().width"
            ),
            zoom_width,
        )
        workspace.get_by_role("button", name="Fit page").click()
        self.assertAlmostEqual(
            canvas.evaluate("(overlay) => overlay.getBoundingClientRect().height"),
            fit_bounds["height"],
            delta=1,
        )
        workspace.locator(".calibration-length").fill("6")
        canvas.scroll_into_view_if_needed()
        workspace.locator(".plan-tracer").evaluate("(frame) => { frame.scrollTop = 0; }")
        def drag_plan_line(x1, y1, x2, y2):
            canvas.scroll_into_view_if_needed()
            plan_frame = workspace.locator(".plan-tracer")
            bounds = canvas.bounding_box()
            self.assertIsNotNone(bounds)
            midpoint_y = (y1 + y2) / 2
            desired_scroll = midpoint_y * bounds["height"] / 800 - 300
            plan_frame.evaluate(
                "(frame, top) => { frame.scrollTop = Math.max(0, Math.min(top, frame.scrollHeight - frame.clientHeight)); }",
                desired_scroll,
            )
            bounds = canvas.bounding_box()
            self.assertIsNotNone(bounds)
            start = {
                "x": bounds["x"] + bounds["width"] * x1 / 600,
                "y": bounds["y"] + bounds["height"] * y1 / 800,
            }
            end = {
                "x": bounds["x"] + bounds["width"] * x2 / 600,
                "y": bounds["y"] + bounds["height"] * y2 / 800,
            }
            self.page.mouse.move(start["x"], start["y"])
            self.page.mouse.down()
            self.page.mouse.move(end["x"], end["y"], steps=8)
            self.page.mouse.up()

        drag_plan_line(80, 100, 520, 100)
        self.assertIn("Scale recorded", workspace.locator(".trace-status").inner_text())
        workspace.locator(".suggest-exterior").click()
        workspace.locator(".accept-outline").wait_for(state="visible")
        self.assertIn(
            "Possible exterior outline",
            workspace.locator(".trace-status").inner_text(),
        )
        workspace.locator(".clear-outline").click()

        workspace.locator(".plan-note-input").fill("Vent")
        workspace.locator('[data-trace-mode="note"]').click()
        workspace.locator(".plan-tracer").evaluate("(frame) => { frame.scrollTop = 0; }")
        canvas.click(position={"x": 180, "y": 120})
        self.assertIn("Vent", workspace.locator(".trace-status").inner_text())
        self.assertEqual(workspace.locator(".saved-annotation-row").count(), 1)
        workspace.locator(".save-geometry").click()
        self.page.wait_for_function(
            "() => document.querySelector('.model-status')?.textContent.includes('Saved 1 plan note')"
        )

        workspace.locator('[data-trace-mode="wall"]').click()
        canvas.scroll_into_view_if_needed()
        workspace.locator(".plan-tracer").evaluate("(frame) => { frame.scrollTop = 0; }")
        drag_plan_line(80, 100, 520, 100)
        self.assertIn("Enter a wall height", workspace.locator(".trace-status").inner_text())
        self.assertEqual(workspace.locator(".saved-wall-row").count(), 0)
        workspace.locator(".trace-height").fill("2.7")
        workspace.locator(".trace-thickness").fill("0.3")
        drag_plan_line(80, 100, 520, 100)
        drag_plan_line(520, 100, 520, 350)
        drag_plan_line(520, 350, 80, 350)
        drag_plan_line(80, 350, 80, 100)
        self.assertEqual(
            workspace.locator(".saved-wall-row").count(),
            4,
            workspace.locator(".trace-status").inner_text(),
        )
        self.assertIn("6.00 m", workspace.locator(".saved-wall-row").first.inner_text())

        first_wall_color = workspace.locator(".saved-wall-row").first.locator(".wall-color")
        first_wall_color.evaluate(
            """input => {
                input.value = "#aa3366";
                input.dispatchEvent(new Event("input", { bubbles: true }));
            }"""
        )
        self.assertFalse(workspace.locator(".save-geometry").is_disabled())
        workspace.locator(".save-geometry").click()
        self.page.wait_for_function(
            """() => {
                const status = document.querySelector('.model-status');
                return status?.classList.contains('error')
                    || status?.textContent.includes('real Three.js GLB model is ready');
            }"""
        )
        self.assertNotIn(
            "error",
            workspace.locator(".model-status").get_attribute("class"),
            workspace.locator(".model-status").inner_text(),
        )
        workspace.locator(".house-model-canvas").wait_for()
        self.assertTrue(workspace.locator(".house-model-canvas").evaluate(
            "canvas => canvas instanceof HTMLCanvasElement && canvas.width > 100"
        ))
        self.assertFalse(workspace.locator(".download-model").is_hidden())
        saved_geometry = self.page.evaluate(
            """async (id) => {
                const plans = await (await fetch("/api/house-plans")).json();
                const response = await fetch(
                    `/api/house-plans/${plans.plans[0].id}/geometry`
                );
                return (await response.json()).geometry;
            }"""
        )
        self.assertIn(
            "#aa3366",
            [wall.get("color") for wall in saved_geometry["walls"]],
        )
        workspace.locator(".model-note-title").fill("Kitchen vent")
        workspace.locator(".model-note-text").fill("Check extractor position")
        workspace.locator('[data-view="top"]').click()
        workspace.locator(".place-model-note").click()
        self.assertIn("Click a visible surface", workspace.locator(".model-status").inner_text())
        viewer_canvas = workspace.locator(".house-model-canvas")
        viewer_bounds = viewer_canvas.bounding_box()
        self.page.mouse.click(
            viewer_bounds["x"] + viewer_bounds["width"] / 2,
            viewer_bounds["y"] + viewer_bounds["height"] / 2,
        )
        workspace.locator(".saved-model-note").wait_for()
        self.assertIn("Kitchen vent", workspace.locator(".saved-model-note").inner_text())
        workspace.locator(".save-geometry").click()
        self.page.wait_for_function(
            "() => document.querySelector('.model-status')?.textContent.includes('real Three.js GLB model is ready')"
        )
        saved_model_notes = self.page.evaluate(
            """async () => {
                const plans = await (await fetch("/api/house-plans")).json();
                const response = await fetch(
                    `/api/house-plans/${plans.plans[0].id}/geometry`
                );
                return (await response.json()).geometry.model_annotations;
            }"""
        )
        self.assertEqual(saved_model_notes[0]["title"], "Kitchen vent")
        self.assertEqual(saved_model_notes[0]["note"], "Check extractor position")
        self.assertTrue(workspace.locator(".model-note-marker").inner_text().find("Kitchen vent") >= 0)
        before_rotation = viewer_canvas.screenshot()
        viewer_bounds = viewer_canvas.bounding_box()
        self.page.mouse.move(
            viewer_bounds["x"] + viewer_bounds["width"] / 2,
            viewer_bounds["y"] + viewer_bounds["height"] / 2,
        )
        self.page.mouse.down()
        self.page.mouse.move(
            viewer_bounds["x"] + viewer_bounds["width"] / 2 + 70,
            viewer_bounds["y"] + viewer_bounds["height"] / 2 - 35,
            steps=8,
        )
        self.page.mouse.up()
        self.page.wait_for_timeout(250)
        self.assertNotEqual(before_rotation, viewer_canvas.screenshot())
        workspace.locator('[data-view="top"]').click()
        workspace.locator('[data-view="wireframe"]').click()
        workspace.locator('[data-view="cutaway"]').click()
        self.assertEqual(self.errors, [])

        self.page.reload()
        saved = self.page.locator(".model-workspace .saved-wall-row")
        saved.first.wait_for()
        self.assertEqual(saved.count(), 4)
        self.assertIn("Exterior wall 1", saved.first.inner_text())
        self.assertIn("Kitchen vent", self.page.locator(".saved-model-note").inner_text())
        self.assertIn(
            "Check extractor position",
            self.page.locator(".model-note-marker").inner_text(),
        )
        self.page.wait_for_function(
            "() => document.querySelector('.plan-reference')?.naturalWidth > 0"
        )
        workspace.locator('[data-trace-mode="wall"]').click()
        workspace.locator(".trace-exterior-input").uncheck()
        workspace.locator(".trace-height").fill("2.7")
        workspace.locator(".trace-thickness").fill("0.3")
        workspace.locator(".plan-tracer").evaluate(
            "(frame) => { frame.scrollTop = frame.scrollHeight; }"
        )
        drag_plan_line(80, 650, 520, 650)
        self.assertIn(
            "Wall added at the calibrated scale",
            workspace.locator(".trace-status").inner_text(),
        )
        self.assertEqual(saved.count(), 5)
        self.assertEqual(self.errors, [])


if __name__ == "__main__":
    unittest.main()
