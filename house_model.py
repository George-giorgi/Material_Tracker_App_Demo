"""GLB generation for explicitly confirmed house-plan wall geometry."""

import json
import math
import re
import struct
import zlib


_MATERIALS = [
    {
        "name": "Exterior plaster",
        "pbrMetallicRoughness": {
            "baseColorFactor": [0.83, 0.79, 0.71, 1],
            "metallicFactor": 0,
            "roughnessFactor": 0.9,
        },
        "doubleSided": True,
    },
    {
        "name": "Interior plaster",
        "pbrMetallicRoughness": {
            "baseColorFactor": [0.95, 0.93, 0.87, 1],
            "metallicFactor": 0,
            "roughnessFactor": 0.88,
        },
        "doubleSided": True,
    },
    {
        "name": "Dark wall tops",
        "pbrMetallicRoughness": {
            "baseColorFactor": [0.13, 0.15, 0.17, 1],
            "metallicFactor": 0,
            "roughnessFactor": 0.75,
        },
        "doubleSided": True,
    },
    {
        "name": "Illustrative oak floor",
        "pbrMetallicRoughness": {
            "baseColorFactor": [0.74, 0.59, 0.43, 1],
            "metallicFactor": 0,
            "roughnessFactor": 0.8,
            "baseColorTexture": {"index": 0},
        },
        "doubleSided": True,
    },
]


def _png_chunk(kind, data):
    content = kind + data
    return struct.pack(">I", len(data)) + content + struct.pack(
        ">I", zlib.crc32(content) & 0xFFFFFFFF
    )


def _wood_texture_png():
    width = height = 256
    pixels = bytearray(width * height * 3)
    colors = (
        (142, 91, 52),
        (158, 105, 62),
        (174, 119, 72),
        (132, 82, 47),
        (163, 106, 62),
    )
    plank_width = 51
    row_height = 64
    for y in range(height):
        row = y // row_height
        local_y = y % row_height
        for x in range(width):
            column = x // plank_width
            local_x = (x + (row % 2) * (plank_width // 2)) % plank_width
            color = colors[(column + row * 3) % len(colors)]
            grain = ((x * 17 + y * 31 + (x * y) % 29) % 7) - 3
            if local_y < 2 or local_x < 2:
                grain -= 16
            if local_y > row_height - 3:
                grain -= 12
            offset = (y * width + x) * 3
            pixels[offset : offset + 3] = bytes(
                max(0, min(255, component + grain)) for component in color
            )
    scanlines = b"".join(
        b"\0" + pixels[row * width * 3 : (row + 1) * width * 3]
        for row in range(height)
    )
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", struct.pack(">2I5B", width, height, 8, 2, 0, 0, 0))
        + _png_chunk(b"IDAT", zlib.compress(scanlines, 9))
        + _png_chunk(b"IEND", b"")
    )


def _floor_outline(walls):
    exterior = [wall for wall in walls if wall["exterior"]]
    if len(exterior) < 3:
        return None

    tolerance = 0.015
    points = []
    edges = []

    def point_index(x, y):
        for index, (point_x, point_y) in enumerate(points):
            if math.hypot(x - point_x, y - point_y) <= tolerance:
                return index
        points.append((x, y))
        return len(points) - 1

    for wall in exterior:
        start = point_index(wall["x1"], wall["y1"])
        end = point_index(wall["x2"], wall["y2"])
        if start == end:
            return None
        edges.append((start, end))

    adjacency = [[] for _ in points]
    for edge_index, (start, end) in enumerate(edges):
        adjacency[start].append(edge_index)
        adjacency[end].append(edge_index)
    if any(len(connected) != 2 for connected in adjacency):
        return None

    ordered = [0]
    previous_edge = -1
    current = 0
    for _ in range(len(edges)):
        edge_index = next(
            (index for index in adjacency[current] if index != previous_edge),
            None,
        )
        if edge_index is None:
            return None
        start, end = edges[edge_index]
        following = end if start == current else start
        if following == ordered[0]:
            if len(ordered) != len(points):
                return None
            break
        if following in ordered:
            return None
        ordered.append(following)
        previous_edge, current = edge_index, following
    else:
        return None

    polygon = [points[index] for index in ordered]
    twice_area = sum(
        polygon[index][0] * polygon[(index + 1) % len(polygon)][1]
        - polygon[(index + 1) % len(polygon)][0] * polygon[index][1]
        for index in range(len(polygon))
    )
    if abs(twice_area) < 0.01:
        return None
    return polygon


def _triangulate(polygon):
    vertices = list(range(len(polygon)))
    signed_area = sum(
        polygon[index][0] * polygon[(index + 1) % len(polygon)][1]
        - polygon[(index + 1) % len(polygon)][0] * polygon[index][1]
        for index in range(len(polygon))
    )
    orientation = 1 if signed_area > 0 else -1

    def cross(a, b, c):
        return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (
            c[0] - a[0]
        )

    triangles = []
    while len(vertices) > 3:
        ear_found = False
        for index, current in enumerate(vertices):
            previous = vertices[index - 1]
            following = vertices[(index + 1) % len(vertices)]
            if orientation * cross(
                polygon[previous], polygon[current], polygon[following]
            ) <= 1e-10:
                continue
            a, b, c = polygon[previous], polygon[current], polygon[following]
            contains_vertex = False
            for other in vertices:
                if other in {previous, current, following}:
                    continue
                p = polygon[other]
                ab = orientation * cross(a, b, p)
                bc = orientation * cross(b, c, p)
                ca = orientation * cross(c, a, p)
                if ab >= -1e-10 and bc >= -1e-10 and ca >= -1e-10:
                    contains_vertex = True
                    break
            if contains_vertex:
                continue
            triangles.append((previous, current, following))
            del vertices[index]
            ear_found = True
            break
        if not ear_found:
            return []
    triangles.append(tuple(vertices))
    return triangles


def _quad(material_faces, material, points, uv=None):
    if uv is None:
        uv = [(0, 0), (1, 0), (1, 1), (0, 1)]
    material_faces.setdefault(material, []).append((points, uv))


def _exterior_left_sides(walls, polygon):
    if polygon is None:
        return {}
    signed_area = sum(
        polygon[index][0] * polygon[(index + 1) % len(polygon)][1]
        - polygon[(index + 1) % len(polygon)][0] * polygon[index][1]
        for index in range(len(polygon))
    )
    left_sides = {}
    for wall in walls:
        if not wall["exterior"]:
            continue
        start = (wall["x1"], wall["y1"])
        end = (wall["x2"], wall["y2"])
        for index, edge_start in enumerate(polygon):
            edge_end = polygon[(index + 1) % len(polygon)]
            same_direction = (
                math.hypot(start[0] - edge_start[0], start[1] - edge_start[1])
                <= 0.015
                and math.hypot(end[0] - edge_end[0], end[1] - edge_end[1])
                <= 0.015
            )
            reverse_direction = (
                math.hypot(start[0] - edge_end[0], start[1] - edge_end[1])
                <= 0.015
                and math.hypot(end[0] - edge_start[0], end[1] - edge_start[1])
                <= 0.015
            )
            if same_direction or reverse_direction:
                left_sides[wall["id"]] = (signed_area > 0) != same_direction
                break
    return left_sides


def _wall_faces(wall, exterior_left=None, wall_material=None):
    dx = wall["x2"] - wall["x1"]
    dy = wall["y2"] - wall["y1"]
    length = math.hypot(dx, dy)
    if length <= 0:
        return None
    half = wall["thickness"] / 2
    nx = -dy / length * half
    ny = dx / length * half
    x1, y1, x2, y2, height = (
        wall["x1"],
        wall["y1"],
        wall["x2"],
        wall["y2"],
        wall["height"],
    )
    a = (x1 + nx, y1 + ny)
    b = (x2 + nx, y2 + ny)
    c = (x2 - nx, y2 - ny)
    d = (x1 - nx, y1 - ny)

    def at(point, z):
        return (point[0], z, point[1])

    material_faces = {}
    side_length = length / 2
    wall_height = height / 2
    left_material = wall_material if wall_material is not None else 1
    right_material = wall_material if wall_material is not None else 1
    if wall["exterior"]:
        if wall_material is not None:
            left_material = right_material = wall_material
        elif exterior_left is None:
            left_material = right_material = 0
        elif exterior_left:
            left_material = 0
        else:
            right_material = 0
    _quad(
        material_faces,
        left_material,
        [at(a, 0), at(b, 0), at(b, height), at(a, height)],
        [(0, 0), (side_length, 0), (side_length, wall_height), (0, wall_height)],
    )
    _quad(
        material_faces,
        right_material,
        [at(c, 0), at(d, 0), at(d, height), at(c, height)],
        [(0, 0), (side_length, 0), (side_length, wall_height), (0, wall_height)],
    )
    _quad(
        material_faces,
        wall_material if wall_material is not None else 1,
        [at(a, 0), at(d, 0), at(d, height), at(a, height)],
    )
    _quad(
        material_faces,
        wall_material if wall_material is not None else 1,
        [at(b, 0), at(c, 0), at(c, height), at(b, height)],
    )
    _quad(
        material_faces,
        2,
        [at(a, height), at(b, height), at(c, height), at(d, height)],
        [(0, 0), (side_length, 0), (side_length, half), (0, half)],
    )
    _quad(
        material_faces,
        wall_material if wall_material is not None else 1,
        [at(d, 0), at(c, 0), at(b, 0), at(a, 0)],
    )
    return material_faces


def _floor_faces(polygon):
    faces = {3: []}
    for triangle in _triangulate(polygon):
        coordinates = [polygon[index] for index in triangle]
        points = [(x, 0.015, y) for x, y in coordinates]
        uv = [(x / 2, y / 2) for x, y in coordinates]
        faces[3].append((points, uv))
    return faces


class _GlbBuilder:
    def __init__(self):
        self.binary = bytearray()
        self.buffer_views = []
        self.accessors = []
        self.meshes = []
        self.nodes = []
        self.materials = list(_MATERIALS)

    def add_wall_material(self, color):
        rgb = [int(color[index : index + 2], 16) / 255 for index in (1, 3, 5)]
        self.materials.append(
            {
                "name": f"Wall marking {color.upper()}",
                "pbrMetallicRoughness": {
                    "baseColorFactor": [*rgb, 1],
                    "metallicFactor": 0,
                    "roughnessFactor": 0.86,
                },
                "doubleSided": True,
            }
        )
        return len(self.materials) - 1

    def _append_data(self, data, target):
        while len(self.binary) % 4:
            self.binary.append(0)
        offset = len(self.binary)
        self.binary.extend(data)
        view = len(self.buffer_views)
        view = {"buffer": 0, "byteOffset": offset, "byteLength": len(data)}
        if target is not None:
            view["target"] = target
        view_index = len(self.buffer_views)
        self.buffer_views.append(view)
        return view_index

    def _attribute(self, data, count, kind, bounds=None):
        packed = struct.pack(f"<{len(data)}f", *data)
        view = self._append_data(packed, 34962)
        accessor = {
            "bufferView": view,
            "componentType": 5126,
            "count": count,
            "type": kind,
        }
        if bounds:
            accessor.update(bounds)
        self.accessors.append(accessor)
        return len(self.accessors) - 1

    def _primitive(self, face_set, material):
        positions = []
        normals = []
        uvs = []
        indices = []
        minimum = [math.inf, math.inf, math.inf]
        maximum = [-math.inf, -math.inf, -math.inf]
        for points, face_uv in face_set:
            start = len(positions) // 3
            a, b, c = points[:3]
            ab = [b[index] - a[index] for index in range(3)]
            ac = [c[index] - a[index] for index in range(3)]
            normal = [
                ab[1] * ac[2] - ab[2] * ac[1],
                ab[2] * ac[0] - ab[0] * ac[2],
                ab[0] * ac[1] - ab[1] * ac[0],
            ]
            magnitude = math.sqrt(sum(component * component for component in normal))
            if magnitude == 0:
                continue
            normal = [component / magnitude for component in normal]
            for index, point in enumerate(points):
                positions.extend(point)
                normals.extend(normal)
                uvs.extend(face_uv[index])
                for axis in range(3):
                    minimum[axis] = min(minimum[axis], point[axis])
                    maximum[axis] = max(maximum[axis], point[axis])
            indices.extend((start, start + 1, start + 2))
            if len(points) == 4:
                indices.extend((start, start + 2, start + 3))
        if not indices:
            return None
        position_accessor = self._attribute(
            positions,
            len(positions) // 3,
            "VEC3",
            {"min": minimum, "max": maximum},
        )
        normal_accessor = self._attribute(normals, len(normals) // 3, "VEC3")
        uv_accessor = self._attribute(uvs, len(uvs) // 2, "VEC2")
        index_view = self._append_data(
            struct.pack(f"<{len(indices)}I", *indices), 34963
        )
        self.accessors.append(
            {
                "bufferView": index_view,
                "componentType": 5125,
                "count": len(indices),
                "type": "SCALAR",
                "min": [min(indices)],
                "max": [max(indices)],
            }
        )
        return {
            "attributes": {
                "POSITION": position_accessor,
                "NORMAL": normal_accessor,
                "TEXCOORD_0": uv_accessor,
            },
            "indices": len(self.accessors) - 1,
            "material": material,
            "mode": 4,
        }

    def add_mesh(self, name, material_faces, extras=None):
        primitives = [
            primitive
            for material, faces in sorted(material_faces.items())
            if (
                primitive := self._primitive(faces, material)
            ) is not None
        ]
        if not primitives:
            return
        mesh_index = len(self.meshes)
        self.meshes.append({"name": name, "primitives": primitives})
        node = {"name": name, "mesh": mesh_index}
        if extras:
            node["extras"] = extras
        self.nodes.append(node)

    def build(self, floor_texture):
        while len(self.binary) % 4:
            self.binary.append(0)
        image_view = self._append_data(floor_texture, None)
        document = {
            "asset": {"version": "2.0", "generator": "BRUSNA confirmed-plan GLB"},
            "scene": 0,
            "scenes": [
                {
                    "nodes": list(range(len(self.nodes))),
                    "extras": {
                        "geometrySource": "User-traced from the uploaded PDF.",
                        "materialsNote": "Floor finish is illustrative; geometry is source traced.",
                        "roofAvailable": False,
                    },
                }
            ],
            "nodes": self.nodes,
            "meshes": self.meshes,
            "materials": self.materials,
            "textures": [{"sampler": 0, "source": 0}],
            "samplers": [{"magFilter": 9729, "minFilter": 9987, "wrapS": 10497, "wrapT": 10497}],
            "images": [{"bufferView": image_view, "mimeType": "image/png"}],
            "buffers": [{"byteLength": len(self.binary)}],
            "bufferViews": self.buffer_views,
            "accessors": self.accessors,
        }
        json_chunk = json.dumps(
            document, separators=(",", ":"), ensure_ascii=True
        ).encode("utf-8")
        json_chunk += b" " * ((-len(json_chunk)) % 4)
        binary_chunk = bytes(self.binary)
        binary_chunk += b"\0" * ((-len(binary_chunk)) % 4)
        total_length = 12 + 8 + len(json_chunk) + 8 + len(binary_chunk)
        return (
            struct.pack("<4sII", b"glTF", 2, total_length)
            + struct.pack("<I4s", len(json_chunk), b"JSON")
            + json_chunk
            + struct.pack("<I4s", len(binary_chunk), b"BIN\0")
            + binary_chunk
        )


def create_house_glb(walls):
    """Return a self-contained GLB with only the supplied measured walls."""
    if not walls:
        raise ValueError("Trace and save at least one wall before building a 3D model.")
    builder = _GlbBuilder()
    polygon = _floor_outline(walls)
    exterior_left_sides = _exterior_left_sides(walls, polygon)
    for wall in walls:
        color = wall.get("color")
        wall_material = (
            builder.add_wall_material(color)
            if isinstance(color, str) and re.fullmatch(r"#[0-9a-fA-F]{6}", color)
            else None
        )
        faces = _wall_faces(
            wall,
            exterior_left_sides.get(wall["id"]),
            wall_material,
        )
        if faces is None:
            continue
        length = math.hypot(wall["x2"] - wall["x1"], wall["y2"] - wall["y1"])
        builder.add_mesh(
            wall["label"],
            faces,
            {
                "wallId": wall["id"],
                "exterior": wall["exterior"],
                **({"color": color.lower()} if wall_material is not None else {}),
                "lengthMetres": length,
                "midpoint": [
                    (wall["x1"] + wall["x2"]) / 2,
                    0,
                    (wall["y1"] + wall["y2"]) / 2,
                ],
                "confirmedGeometry": True,
                "estimated": False,
            },
        )

    floor_added = False
    if polygon is not None:
        floor_faces = _floor_faces(polygon)
        if floor_faces[3]:
            builder.add_mesh(
                "Measured exterior footprint floor",
                floor_faces,
                {"geometrySource": "closed traced exterior wall loop", "finishIllustrative": True},
            )
            floor_added = True
    if not builder.nodes:
        raise ValueError("No valid wall geometry was available to build the model.")
    return builder.build(_wood_texture_png()), floor_added
