bl_info = {
    "name": "Mod 反向导入器",
    "author": "Codex",
    "version": (0, 1, 0),
    "blender": (3, 6, 0),
    "location": "文件 > 导入 > Mod 文件夹（.ini）",
    "description": "将导出的 mod 文件夹反向导入为 Blender 网格。",
    "category": "Import-Export",
}

import math
import re
import struct
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import bpy
from bpy.props import BoolProperty, EnumProperty, StringProperty
from bpy.types import Operator, Panel, PropertyGroup
from bpy_extras.io_utils import ImportHelper


GAME_PRESETS = (
    ("GIMI", "原神（GIMI）", "原神 / GIMI 风格缓冲区"),
    ("SRMI", "崩坏：星穹铁道（SRMI）", "星铁 / SRMI 风格缓冲区"),
    ("ZZMI", "绝区零（ZZMI）", "绝区零 / ZZMI 风格缓冲区"),
    ("WWMI", "鸣潮（WWMI）", "鸣潮 / WWMI Tools 风格共享缓冲区"),
    ("HI3", "崩坏 3", "崩坏 3 风格缓冲区"),
)


@dataclass
class Resource:
    section: str
    filename: str = ""
    stride: int = 0
    fmt: str = ""
    type: str = ""

    @property
    def path_name(self) -> str:
        return self.filename.replace("\\", "/")

    @property
    def stem(self) -> str:
        return Path(self.path_name).stem


@dataclass
class DrawCall:
    resource: str
    count: int
    start: int
    base_vertex: int
    label: str = ""


@dataclass
class Component:
    name: str
    position: Resource
    blend: Resource | None = None
    texcoord: Resource | None = None
    ibs: list[Resource] = field(default_factory=list)


class ReverseImportError(Exception):
    pass


def strip_inline_comment(line: str) -> str:
    for marker in (";", "#", "；"):
        idx = line.find(marker)
        if idx >= 0:
            return line[:idx]
    return line


def clean_label(line: str) -> str:
    label = line.strip().lstrip(";；#").strip()
    label = re.sub(r"\s+", " ", label)
    label = re.sub(r"[^\w\u4e00-\u9fff.-]+", "_", label, flags=re.UNICODE).strip("_")
    return label[:48]


def natural_name(name: str) -> str:
    name = re.sub(r"(?i)(Position|Texcoord|TexCoord|Blend|IB|AIB)$", "", name)
    return name.strip("_-.") or name


def strip_suffix_ci(value: str, suffix: str) -> str:
    if value.lower().endswith(suffix.lower()):
        return value[: -len(suffix)]
    return value


def parse_ini(path: Path) -> tuple[dict[str, Resource], dict[str, list[DrawCall]]]:
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    resources: dict[str, Resource] = {}
    draw_calls: dict[str, list[DrawCall]] = defaultdict(list)
    current_section = ""
    current_ib = ""
    pending_label = ""
    section_re = re.compile(r"^\s*\[([^\]]+)\]")
    draw_re = re.compile(r"drawindexed\s*=\s*(\d+)\s*,\s*(\d+)\s*,\s*(-?\d+)", re.I)
    ib_re = re.compile(r"\bib\s*=\s*(Resource[^\s;#；]+)", re.I)

    for raw_line in text.splitlines():
        section_match = section_re.match(raw_line)
        if section_match:
            current_section = section_match.group(1)
            current_ib = ""
            pending_label = ""
            if current_section.lower().startswith("resource") and current_section not in resources:
                resources[current_section] = Resource(section=current_section)
            continue

        stripped = raw_line.strip()
        if stripped.startswith((";", "#", "；")):
            label = clean_label(stripped)
            if label:
                pending_label = label
            continue

        line = strip_inline_comment(raw_line).strip()
        if not line:
            continue

        if current_section.lower().startswith("resource") and "=" in line:
            key, value = [part.strip() for part in line.split("=", 1)]
            resource = resources[current_section]
            key = key.lower()
            if key == "filename":
                resource.filename = value
            elif key == "stride":
                try:
                    resource.stride = int(value, 0)
                except ValueError:
                    resource.stride = 0
            elif key == "format":
                resource.fmt = value
            elif key == "type":
                resource.type = value
            continue

        if not current_section.lower().startswith("textureoverride"):
            continue

        ib_match = ib_re.search(line)
        if ib_match:
            current_ib = ib_match.group(1)
            continue

        draw_match = draw_re.search(line)
        if draw_match and current_ib:
            count, start, base_vertex = map(int, draw_match.groups())
            draw_calls[current_ib].append(
                DrawCall(current_ib, count, start, base_vertex, pending_label)
            )
            pending_label = ""

    return resources, draw_calls


def parse_wwmi_draw_calls(path: Path) -> list[DrawCall]:
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    draw_calls: list[DrawCall] = []
    current_section = ""
    pending_label = ""
    section_re = re.compile(r"^\s*\[([^\]]+)\]")
    draw_re = re.compile(r"drawindexed\s*=\s*(\d+)\s*,\s*(\d+)\s*,\s*(-?\d+)", re.I)

    for raw_line in text.splitlines():
        section_match = section_re.match(raw_line)
        if section_match:
            current_section = section_match.group(1)
            pending_label = ""
            continue

        stripped = raw_line.strip()
        if stripped.startswith((";", "#", "；")):
            label = clean_label(stripped)
            if label:
                label = re.sub(r"(?i)^draw_component_", "Component_", label)
                pending_label = label
            continue

        if not current_section.lower().startswith("textureoverridecomponent"):
            continue

        line = strip_inline_comment(raw_line).strip()
        draw_match = draw_re.search(line)
        if draw_match:
            count, start, base_vertex = map(int, draw_match.groups())
            section_label = current_section.replace("TextureOverride", "")
            label = pending_label or f"{section_label}_{len(draw_calls):02d}"
            draw_calls.append(
                DrawCall("ResourceIndexBuffer", count, start, base_vertex, label)
            )
            pending_label = ""

    return draw_calls


def find_resource_by_stem(resources: dict[str, Resource], stem: str) -> Resource | None:
    stem_lower = stem.lower()
    for resource in resources.values():
        if resource.stem.lower() == stem_lower:
            return resource
    return None


def find_resource_by_section(resources: dict[str, Resource], section: str) -> Resource | None:
    section_lower = section.lower()
    for resource in resources.values():
        if resource.section.lower() == section_lower:
            return resource
    return None


def find_associated_resource(
    resources: dict[str, Resource],
    base_raw: str,
    suffix: str,
) -> Resource | None:
    base_clean = base_raw.strip("_-.")
    candidates = [
        base_raw + suffix,
        base_raw.rstrip("_-.") + suffix,
        base_clean + suffix,
    ]
    for candidate in candidates:
        found = find_resource_by_stem(resources, candidate)
        if found is not None:
            return found
    for candidate in candidates:
        found = find_resource_by_section(resources, "Resource" + candidate)
        if found is not None:
            return found
    return None


def is_index_resource(resource: Resource) -> bool:
    return bool(
        resource.filename
        and (
            resource.path_name.lower().endswith(".ib")
            or resource.fmt.upper().endswith("_UINT")
        )
    )


def discover_components(
    resources: dict[str, Resource],
    draw_calls: dict[str, list[DrawCall]] | None = None,
) -> list[Component]:
    components: list[Component] = []
    seen_bases: set[str] = set()
    ib_resources = [res for res in resources.values() if is_index_resource(res)]
    referenced_ib_sections = {
        section.lower()
        for section, calls in (draw_calls or {}).items()
        if len(calls) > 0
    }

    for res in resources.values():
        if not res.filename or not res.path_name.lower().endswith("position.buf"):
            continue

        base_raw = strip_suffix_ci(res.stem, "Position")
        base = base_raw.strip("_-.") or base_raw
        if base.lower() in seen_bases:
            continue
        seen_bases.add(base.lower())
        blend = (
            find_associated_resource(resources, base_raw, "Blend")
        )
        texcoord = (
            find_associated_resource(resources, base_raw, "Texcoord")
            or find_associated_resource(resources, base_raw, "TexCoord")
        )

        matching_ibs = [
            ib
            for ib in ib_resources
            if ib.stem.lower().startswith(base_raw.lower())
            or ib.stem.lower().startswith(base.lower())
        ]
        if referenced_ib_sections:
            matching_ibs = [
                ib
                for ib in matching_ibs
                if ib.section.lower() in referenced_ib_sections
            ]
        ibs = matching_ibs
        components.append(Component(base, res, blend, texcoord, ibs))

    return components


def read_bytes(folder: Path, resource: Resource) -> bytes:
    path = folder / resource.path_name
    if not path.is_file():
        raise ReverseImportError(f"找不到资源文件：{path}")
    return path.read_bytes()


def read_f32(data: bytes, offset: int) -> float:
    return struct.unpack_from("<f", data, offset)[0]


def read_u32(data: bytes, offset: int) -> int:
    return struct.unpack_from("<I", data, offset)[0]


def read_u16(data: bytes, offset: int) -> int:
    return struct.unpack_from("<H", data, offset)[0]


def half_to_float(bits: int) -> float:
    sign = -1.0 if bits & 0x8000 else 1.0
    exponent = (bits >> 10) & 0x1F
    fraction = bits & 0x03FF
    if exponent == 0:
        return sign * (2.0**-14) * (fraction / 1024.0)
    if exponent == 31:
        return math.nan if fraction else sign * math.inf
    return sign * (2.0 ** (exponent - 15)) * (1.0 + fraction / 1024.0)


def snorm8_to_float(value: int) -> float:
    signed = value - 256 if value >= 128 else value
    return max(-1.0, signed / 127.0)


def unorm8_to_float(value: int) -> float:
    return value / 255.0


def unorm16_to_float(value: int) -> float:
    return value / 65535.0


def decode_position(data: bytes, stride: int) -> tuple[list[tuple[float, float, float]], list[tuple[float, float, float]], list[tuple[float, float, float, float]]]:
    if stride <= 0:
        raise ReverseImportError("Position 资源缺少 stride")
    if len(data) % stride != 0:
        raise ReverseImportError(f"Position 缓冲区长度 {len(data)} 无法被 stride {stride} 整除")

    vertices = len(data) // stride
    positions: list[tuple[float, float, float]] = []
    normals: list[tuple[float, float, float]] = []
    tangents: list[tuple[float, float, float, float]] = []

    for index in range(vertices):
        base = index * stride
        if stride >= 40:
            positions.append((read_f32(data, base), read_f32(data, base + 4), read_f32(data, base + 8)))
            normals.append((read_f32(data, base + 12), read_f32(data, base + 16), read_f32(data, base + 20)))
            tangents.append((read_f32(data, base + 24), read_f32(data, base + 28), read_f32(data, base + 32), read_f32(data, base + 36)))
        elif stride >= 24:
            positions.append((read_f32(data, base), read_f32(data, base + 4), read_f32(data, base + 8)))
            normals.append((read_f32(data, base + 12), read_f32(data, base + 16), read_f32(data, base + 20)))
            tangents.append((0.0, 0.0, 0.0, 1.0))
        elif stride >= 12:
            positions.append((read_f32(data, base), read_f32(data, base + 4), read_f32(data, base + 8)))
            normals.append((0.0, 0.0, 1.0))
            tangents.append((0.0, 0.0, 0.0, 1.0))
        else:
            raise ReverseImportError(f"暂不支持的 Position stride：{stride}")

    return positions, normals, tangents


def decode_blend(data: bytes | None, stride: int, vertices: int) -> tuple[list[tuple[float, ...]], list[tuple[int, ...]]]:
    weights = [(1.0, 0.0, 0.0, 0.0) for _ in range(vertices)]
    indices = [(0, 0, 0, 0) for _ in range(vertices)]
    if not data or stride <= 0:
        return weights, indices
    if len(data) % stride != 0:
        raise ReverseImportError(f"Blend 缓冲区长度 {len(data)} 无法被 stride {stride} 整除")

    count = min(vertices, len(data) // stride)
    for index in range(count):
        base = index * stride
        if stride >= 32:
            weights[index] = (
                read_f32(data, base),
                read_f32(data, base + 4),
                read_f32(data, base + 8),
                read_f32(data, base + 12),
            )
            indices[index] = (
                read_u32(data, base + 16),
                read_u32(data, base + 20),
                read_u32(data, base + 24),
                read_u32(data, base + 28),
            )
        elif stride >= 16:
            weights[index] = tuple(data[base + i] / 255.0 for i in range(4))
            indices[index] = tuple(data[base + 4 + i] for i in range(4))
    return weights, indices


def valid_uv_pair(u: float, v: float, generous: bool = False) -> bool:
    if not (math.isfinite(u) and math.isfinite(v)):
        return False
    limit = 8.0 if generous else 2.5
    return -limit <= u <= limit and -limit <= v <= limit


def add_uv_candidate(candidates: dict[str, list[tuple[float, float]]], name: str, data: list[tuple[float, float]], generous: bool = False) -> None:
    if not data:
        return
    valid = sum(1 for u, v in data if valid_uv_pair(u, v, generous))
    if valid >= max(1, len(data) * 0.8):
        candidates[name] = data


def decode_texcoord(data: bytes | None, stride: int, vertices: int, game: str) -> tuple[dict[str, list[tuple[float, float]]], list[tuple[float, float, float, float]] | None]:
    candidates: dict[str, list[tuple[float, float]]] = {}
    colors: list[tuple[float, float, float, float]] | None = None
    if not data or stride <= 0:
        return candidates, colors
    if len(data) % stride != 0:
        raise ReverseImportError(f"TexCoord 缓冲区长度 {len(data)} 无法被 stride {stride} 整除")

    count = min(vertices, len(data) // stride)
    if stride >= 4:
        colors = []
        for index in range(count):
            base = index * stride
            colors.append(tuple(data[base + channel] / 255.0 for channel in range(4)))
        while len(colors) < vertices:
            colors.append((1.0, 1.0, 1.0, 1.0))

    if game in {"ZZMI", "SRMI"} and stride == 8:
        uv = []
        for index in range(count):
            base = index * stride + 4
            uv.append((half_to_float(read_u16(data, base)), half_to_float(read_u16(data, base + 2))))
        add_uv_candidate(candidates, "TEXCOORD.xy", uv, generous=True)
    elif stride == 12:
        uv = []
        for index in range(count):
            base = index * stride
            uv.append((read_f32(data, base + 4), read_f32(data, base + 8)))
        add_uv_candidate(candidates, "TEXCOORD.xy", uv, generous=False)
    elif game in {"ZZMI", "SRMI"} and stride in {20, 24, 28, 32}:
        for offset, name in ((4, "TEXCOORD.xy"), (stride - 4, "TEXCOORD1.xy")):
            uv = []
            for index in range(count):
                base = index * stride + offset
                uv.append((half_to_float(read_u16(data, base)), half_to_float(read_u16(data, base + 2))))
            add_uv_candidate(candidates, name, uv, generous=True)
        if stride >= 16:
            uv = []
            for index in range(count):
                base = index * stride
                uv.append((read_f32(data, base + 8), read_f32(data, base + 12)))
            add_uv_candidate(candidates, "TEXCOORD_float08.xy", uv, generous=True)
    else:
        for offset in range(0, stride - 7, 4):
            uv = []
            for index in range(count):
                base = index * stride + offset
                uv.append((read_f32(data, base), read_f32(data, base + 4)))
            add_uv_candidate(candidates, f"TEXCOORD_f32_{offset:02d}.xy", uv, generous=True)

    for name, uv in list(candidates.items()):
        while len(uv) < vertices:
            uv.append((0.0, 0.0))
        candidates[name] = uv
    return candidates, colors


def decode_wwmi_vector(data: bytes | None, stride: int, vertices: int) -> list[tuple[float, float, float]]:
    normals = [(0.0, 0.0, 1.0) for _ in range(vertices)]
    if not data or stride <= 0:
        return normals
    if len(data) % stride != 0:
        raise ReverseImportError(f"Vector 缓冲区长度 {len(data)} 无法被 stride {stride} 整除")
    count = min(vertices, len(data) // stride)
    for index in range(count):
        base = index * stride
        if stride >= 8:
            normals[index] = (
                snorm8_to_float(data[base + 4]),
                snorm8_to_float(data[base + 5]),
                snorm8_to_float(data[base + 6]),
            )
    return normals


def decode_wwmi_blend(
    data: bytes | None,
    stride: int,
    vertices: int,
    remap_data: bytes | None = None,
    remap_stride: int = 0,
) -> tuple[list[tuple[float, ...]], list[tuple[int, ...]]]:
    weights = [(1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0) for _ in range(vertices)]
    indices = [(0, 0, 0, 0, 0, 0, 0, 0) for _ in range(vertices)]
    if not data or stride <= 0:
        return weights, indices
    if len(data) % stride != 0:
        raise ReverseImportError(f"Blend 缓冲区长度 {len(data)} 无法被 stride {stride} 整除")

    count = min(vertices, len(data) // stride)
    for index in range(count):
        base = index * stride
        if stride >= 16:
            indices[index] = tuple(data[base + i] for i in range(8))
            raw_weights = [data[base + 8 + i] / 255.0 for i in range(8)]
            total = sum(raw_weights)
            if total > 0.0:
                raw_weights = [weight / total for weight in raw_weights]
            weights[index] = tuple(raw_weights)

    if remap_data:
        if remap_stride <= 0:
            remap_stride = len(remap_data) // vertices if vertices else 0
        if remap_stride >= 16 and len(remap_data) >= remap_stride * min(vertices, len(remap_data) // remap_stride):
            remap_count = min(vertices, len(remap_data) // remap_stride)
            for index in range(remap_count):
                base = index * remap_stride
                indices[index] = tuple(read_u16(remap_data, base + i * 2) for i in range(8))
    return weights, indices


def decode_wwmi_texcoord(data: bytes | None, stride: int, vertices: int) -> tuple[dict[str, list[tuple[float, float]]], list[tuple[float, float, float, float]] | None]:
    uv_sets: dict[str, list[tuple[float, float]]] = {}
    color1: list[tuple[float, float, float, float]] | None = None
    if not data or stride <= 0:
        return uv_sets, color1
    if len(data) % stride != 0:
        raise ReverseImportError(f"TexCoord 缓冲区长度 {len(data)} 无法被 stride {stride} 整除")

    count = min(vertices, len(data) // stride)
    specs = [
        ("TEXCOORD.xy", 0),
        ("TEXCOORD1.xy", 8),
        ("TEXCOORD2.xy", 12),
    ]
    for name, offset in specs:
        if stride < offset + 4:
            continue
        uv = []
        for index in range(count):
            base = index * stride + offset
            uv.append((half_to_float(read_u16(data, base)), half_to_float(read_u16(data, base + 2))))
        while len(uv) < vertices:
            uv.append((0.0, 0.0))
        uv_sets[name] = uv

    if stride >= 8:
        color1 = []
        for index in range(count):
            base = index * stride + 4
            color1.append(
                (
                    unorm16_to_float(read_u16(data, base)),
                    unorm16_to_float(read_u16(data, base + 2)),
                    0.0,
                    1.0,
                )
            )
        while len(color1) < vertices:
            color1.append((0.0, 0.0, 0.0, 1.0))

    return uv_sets, color1


def decode_wwmi_color(data: bytes | None, stride: int, vertices: int) -> list[tuple[float, float, float, float]] | None:
    if not data or stride <= 0:
        return None
    if len(data) % stride != 0:
        raise ReverseImportError(f"Color 缓冲区长度 {len(data)} 无法被 stride {stride} 整除")
    count = min(vertices, len(data) // stride)
    colors = []
    for index in range(count):
        base = index * stride
        if stride >= 4:
            colors.append(tuple(unorm8_to_float(data[base + channel]) for channel in range(4)))
    while len(colors) < vertices:
        colors.append((1.0, 1.0, 1.0, 1.0))
    return colors


def decode_indices(data: bytes, fmt: str) -> list[int]:
    upper_fmt = fmt.upper()
    if "R16" in upper_fmt:
        if len(data) % 2 != 0:
            raise ReverseImportError("R16 索引缓冲区字节长度不是偶数")
        return list(struct.unpack("<" + "H" * (len(data) // 2), data))
    if len(data) % 4 != 0:
        raise ReverseImportError("R32 索引缓冲区长度无法被 4 整除")
    return list(struct.unpack("<" + "I" * (len(data) // 4), data))


def make_object_name(component: str, ib: str, draw: DrawCall | None, draw_index: int) -> str:
    base = natural_name(ib)
    if not draw:
        return base
    label = draw.label or f"draw_{draw_index:02d}"
    return f"{base}_{label}"


def remap_faces(indices: list[int], vertex_count: int, flip_winding: bool) -> tuple[list[int], list[tuple[int, int, int]]]:
    used: dict[int, int] = {}
    old_order: list[int] = []
    faces: list[tuple[int, int, int]] = []

    for face_start in range(0, len(indices) - 2, 3):
        tri_old = indices[face_start : face_start + 3]
        if any(i < 0 or i >= vertex_count for i in tri_old):
            continue
        tri_new = []
        for old in tri_old:
            if old not in used:
                used[old] = len(old_order)
                old_order.append(old)
            tri_new.append(used[old])
        if flip_winding:
            tri_new = [tri_new[2], tri_new[1], tri_new[0]]
        if len(set(tri_new)) == 3:
            faces.append(tuple(tri_new))
    return old_order, faces


def assign_uv_layers(mesh: bpy.types.Mesh, old_order: list[int], uv_sets: dict[str, list[tuple[float, float]]], flip_v: bool) -> None:
    if not uv_sets:
        return
    for name, uv_data in uv_sets.items():
        layer = mesh.uv_layers.new(name=name)
        for loop in mesh.loops:
            old_vertex = old_order[loop.vertex_index]
            u, v = uv_data[old_vertex]
            layer.data[loop.index].uv = (u, 1.0 - v if flip_v else v)


def assign_colors(mesh: bpy.types.Mesh, old_order: list[int], colors: list[tuple[float, float, float, float]] | None) -> None:
    if not colors:
        return
    try:
        color_attr = mesh.color_attributes.new(name="COLOR", type="BYTE_COLOR", domain="CORNER")
        for loop in mesh.loops:
            color_attr.data[loop.index].color = colors[old_order[loop.vertex_index]]
    except Exception:
        try:
            color_layer = mesh.vertex_colors.new(name="COLOR")
            for loop in mesh.loops:
                color_layer.data[loop.index].color = colors[old_order[loop.vertex_index]]
        except Exception:
            pass


def assign_normals(mesh: bpy.types.Mesh, old_order: list[int], normals: list[tuple[float, float, float]]) -> None:
    if not normals:
        return
    loop_normals = [normals[old_order[loop.vertex_index]] for loop in mesh.loops]
    if not loop_normals:
        return
    for poly in mesh.polygons:
        poly.use_smooth = True
    try:
        mesh.normals_split_custom_set(loop_normals)
        if hasattr(mesh, "use_auto_smooth"):
            mesh.use_auto_smooth = True
    except Exception:
        try:
            local_normals = [normals[old] for old in old_order]
            mesh.normals_split_custom_set_from_vertices(local_normals)
            if hasattr(mesh, "use_auto_smooth"):
                mesh.use_auto_smooth = True
        except Exception:
            pass


def assign_vertex_groups(obj: bpy.types.Object, old_order: list[int], weights: list[tuple[float, ...]], indices: list[tuple[int, ...]]) -> None:
    group_ids = sorted(
        {
            int(group_id)
            for old in old_order
            for group_id, weight in zip(indices[old], weights[old])
            if weight > 0.0
        }
    )
    groups = {group_id: obj.vertex_groups.new(name=str(group_id)) for group_id in group_ids}

    for local_index, old in enumerate(old_order):
        for group_id, weight in zip(indices[old], weights[old]):
            if weight <= 0.0:
                continue
            group = groups.get(int(group_id))
            if group is not None:
                group.add((local_index,), float(weight), "REPLACE")


def import_component(
    context: bpy.types.Context,
    collection: bpy.types.Collection,
    folder: Path,
    component: Component,
    draw_calls: dict[str, list[DrawCall]],
    game: str,
    split_draw_calls: bool,
    flip_winding: bool,
    flip_uv_v: bool,
) -> int:
    pos_data = read_bytes(folder, component.position)
    positions, normals, tangents = decode_position(pos_data, component.position.stride)
    vertex_count = len(positions)

    blend_data = read_bytes(folder, component.blend) if component.blend else None
    weights, blend_indices = decode_blend(blend_data, component.blend.stride if component.blend else 0, vertex_count)

    tex_data = read_bytes(folder, component.texcoord) if component.texcoord else None
    uv_sets, colors = decode_texcoord(tex_data, component.texcoord.stride if component.texcoord else 0, vertex_count, game)

    imported = 0
    for ib in component.ibs:
        ib_path = folder / ib.path_name
        if not ib_path.is_file():
            continue
        all_indices = decode_indices(read_bytes(folder, ib), ib.fmt or "DXGI_FORMAT_R32_UINT")
        ib_draw_calls = draw_calls.get(ib.section, [])
        slices: list[tuple[DrawCall | None, list[int]]] = []
        if split_draw_calls and ib_draw_calls:
            for draw in ib_draw_calls:
                start = max(0, draw.start)
                end = min(len(all_indices), start + max(0, draw.count))
                if end > start:
                    adjusted = [idx + draw.base_vertex for idx in all_indices[start:end]]
                    slices.append((draw, adjusted))
        if not slices:
            slices.append((None, all_indices))

        for draw_index, (draw, indices_slice) in enumerate(slices):
            old_order, faces = remap_faces(indices_slice, vertex_count, flip_winding)
            if not faces:
                continue

            object_name = make_object_name(component.name, ib.stem, draw, draw_index)
            local_positions = [positions[old] for old in old_order]
            mesh = bpy.data.meshes.new(object_name)
            mesh.from_pydata(local_positions, [], faces)
            mesh.update(calc_edges=True)

            obj = bpy.data.objects.new(object_name, mesh)
            collection.objects.link(obj)

            assign_uv_layers(mesh, old_order, uv_sets, flip_uv_v)
            assign_colors(mesh, old_order, colors)
            assign_normals(mesh, old_order, normals)
            assign_vertex_groups(obj, old_order, weights, blend_indices)

            obj["Reverse:Component"] = component.name
            obj["Reverse:Position"] = component.position.filename
            if component.blend:
                obj["Reverse:Blend"] = component.blend.filename
            if component.texcoord:
                obj["Reverse:TexCoord"] = component.texcoord.filename
            obj["Reverse:IB"] = ib.filename
            obj["Reverse:Game"] = game
            obj["Reverse:VertexCount"] = vertex_count
            if tangents:
                obj["Reverse:TangentSource"] = "导入缓冲区包含切线；Blender 会在需要时根据 UV 重新计算切线。"

            imported += 1

    return imported


def require_resource(resources: dict[str, Resource], section: str) -> Resource:
    resource = find_resource_by_section(resources, section)
    if resource is None or not resource.filename:
        raise ReverseImportError(f"缺少 WWMI 资源：{section}")
    return resource


def import_wwmi_mod(
    context: bpy.types.Context,
    folder: Path,
    ini_path: Path,
    resources: dict[str, Resource],
    split_draw_calls: bool,
    flip_winding: bool,
    flip_uv_v: bool,
) -> int:
    position_resource = require_resource(resources, "ResourcePositionBuffer")
    index_resource = require_resource(resources, "ResourceIndexBuffer")
    vector_resource = find_resource_by_section(resources, "ResourceVectorBuffer")
    texcoord_resource = find_resource_by_section(resources, "ResourceTexcoordBuffer")
    color_resource = find_resource_by_section(resources, "ResourceColorBuffer")
    blend_resource = find_resource_by_section(resources, "ResourceBlendBuffer")
    blend_remap_vg_resource = find_resource_by_section(resources, "ResourceBlendRemapVertexVGBuffer")

    positions, _, _ = decode_position(read_bytes(folder, position_resource), position_resource.stride)
    vertex_count = len(positions)

    normals = decode_wwmi_vector(
        read_bytes(folder, vector_resource) if vector_resource and vector_resource.filename else None,
        vector_resource.stride if vector_resource else 0,
        vertex_count,
    )
    uv_sets, color1 = decode_wwmi_texcoord(
        read_bytes(folder, texcoord_resource) if texcoord_resource and texcoord_resource.filename else None,
        texcoord_resource.stride if texcoord_resource else 0,
        vertex_count,
    )
    colors = decode_wwmi_color(
        read_bytes(folder, color_resource) if color_resource and color_resource.filename else None,
        color_resource.stride if color_resource else 0,
        vertex_count,
    ) or color1
    weights, blend_indices = decode_wwmi_blend(
        read_bytes(folder, blend_resource) if blend_resource and blend_resource.filename else None,
        blend_resource.stride if blend_resource else 0,
        vertex_count,
        read_bytes(folder, blend_remap_vg_resource) if blend_remap_vg_resource and blend_remap_vg_resource.filename else None,
        blend_remap_vg_resource.stride if blend_remap_vg_resource else 0,
    )
    all_indices = decode_indices(read_bytes(folder, index_resource), index_resource.fmt or "DXGI_FORMAT_R32_UINT")

    draw_calls = parse_wwmi_draw_calls(ini_path)
    if not draw_calls or not split_draw_calls:
        draw_calls = [DrawCall("ResourceIndexBuffer", len(all_indices), 0, 0, "WWMI_完整网格")]

    root_collection = bpy.data.collections.new(f"WWMI 反向导入 - {folder.name}")
    context.scene.collection.children.link(root_collection)

    imported = 0
    for draw_index, draw in enumerate(draw_calls):
        start = max(0, draw.start)
        end = min(len(all_indices), start + max(0, draw.count))
        if end <= start:
            continue

        indices_slice = [index + draw.base_vertex for index in all_indices[start:end]]
        old_order, faces = remap_faces(indices_slice, vertex_count, flip_winding)
        if not faces:
            continue

        object_name = draw.label or f"WWMI_片段_{draw_index:02d}"
        local_positions = [positions[old] for old in old_order]
        mesh = bpy.data.meshes.new(object_name)
        mesh.from_pydata(local_positions, [], faces)
        mesh.update(calc_edges=True)

        obj = bpy.data.objects.new(object_name, mesh)
        root_collection.objects.link(obj)

        assign_uv_layers(mesh, old_order, uv_sets, flip_uv_v)
        assign_colors(mesh, old_order, colors)
        assign_normals(mesh, old_order, normals)
        assign_vertex_groups(obj, old_order, weights, blend_indices)

        obj["Reverse:Game"] = "WWMI"
        obj["Reverse:Position"] = position_resource.filename
        obj["Reverse:IB"] = index_resource.filename
        obj["Reverse:VertexCount"] = vertex_count
        imported += 1

    if imported == 0:
        bpy.data.collections.remove(root_collection)
        raise ReverseImportError("已找到 WWMI 共享缓冲区，但没有任何 drawindexed 片段生成面")

    return imported


def run_import(
    context: bpy.types.Context,
    mod_path: str,
    game: str,
    split_draw_calls: bool,
    flip_winding: bool,
    flip_uv_v: bool,
) -> int:
    folder = Path(bpy.path.abspath(mod_path)).expanduser()
    if folder.is_file() and folder.suffix.lower() == ".ini":
        ini_path = folder
        folder = folder.parent
    else:
        ini_files = sorted(folder.glob("*.ini"))
        if not ini_files:
            raise ReverseImportError(f"在 {folder} 中没有找到 .ini 文件")
        ini_path = ini_files[0]

    resources, draw_calls = parse_ini(ini_path)
    preset = game
    if preset == "WWMI":
        return import_wwmi_mod(
            context,
            folder,
            ini_path,
            resources,
            split_draw_calls,
            flip_winding,
            flip_uv_v,
        )

    components = discover_components(resources, draw_calls)
    if not components:
        raise ReverseImportError(f"在 {folder} 中没有找到可导入的 Position/Blend/TexCoord 网格资源")

    root_collection = bpy.data.collections.new(f"反向导入 - {folder.name}")
    context.scene.collection.children.link(root_collection)

    imported = 0
    for component in components:
        component_collection = bpy.data.collections.new(component.name)
        root_collection.children.link(component_collection)
        imported += import_component(
            context,
            component_collection,
            folder,
            component,
            draw_calls,
            preset,
            split_draw_calls,
            flip_winding,
            flip_uv_v,
        )

    if imported == 0:
        bpy.data.collections.remove(root_collection)
        raise ReverseImportError("已找到网格资源，但没有任何可读取的索引片段生成面")

    return imported


class ReverseSettings(PropertyGroup):
    mod_path: StringProperty(
        name="Mod 文件夹",
        subtype="DIR_PATH",
        description="包含 mod .ini 和缓冲区文件的文件夹",
        default="",
    )
    game: EnumProperty(
        name="游戏预设",
        items=GAME_PRESETS,
        default="GIMI",
    )
    split_draw_calls: BoolProperty(
        name="按 DrawIndexed 拆分",
        description="按 ini 中的 drawindexed 范围创建独立 Blender 对象",
        default=True,
    )
    flip_winding: BoolProperty(
        name="翻转面朝向",
        description="导入时反转三角形顶点顺序",
        default=False,
    )
    flip_uv_v: BoolProperty(
        name="翻转 UV V 轴",
        description="以 V = 1 - V 的方式导入 UV",
        default=False,
    )


class REVERSE_OT_import(Operator):
    bl_idname = "mod_reverse.import_mod"
    bl_label = "导入 Mod"
    bl_options = {"REGISTER", "UNDO"}

    filepath: StringProperty(default="")
    game: EnumProperty(name="游戏预设", items=GAME_PRESETS, default="GIMI")
    split_draw_calls: BoolProperty(name="按 DrawIndexed 拆分", default=True)
    flip_winding: BoolProperty(name="翻转面朝向", default=False)
    flip_uv_v: BoolProperty(name="翻转 UV V 轴", default=False)

    def execute(self, context):
        settings = context.scene.mod_reverse
        mod_path = self.filepath or settings.mod_path
        if not mod_path:
            self.report({"ERROR"}, "请先选择 mod 文件夹或 .ini 文件")
            return {"CANCELLED"}

        try:
            imported = run_import(
                context,
                mod_path,
                self.game or settings.game,
                self.split_draw_calls,
                self.flip_winding,
                self.flip_uv_v,
            )
        except ReverseImportError as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        except Exception as exc:
            self.report({"ERROR"}, f"导入时发生未预期错误：{exc}")
            return {"CANCELLED"}

        self.report({"INFO"}, f"已导入 {imported} 个网格对象")
        return {"FINISHED"}


class IMPORT_SCENE_OT_mod_reverse(Operator, ImportHelper):
    bl_idname = "import_scene.mod_reverse"
    bl_label = "导入 Mod 文件夹"
    bl_options = {"REGISTER", "UNDO"}

    filename_ext = ".ini"
    filter_glob: StringProperty(default="*.ini", options={"HIDDEN"})
    game: EnumProperty(name="游戏预设", items=GAME_PRESETS, default="GIMI")
    split_draw_calls: BoolProperty(name="按 DrawIndexed 拆分", default=True)
    flip_winding: BoolProperty(name="翻转面朝向", default=False)
    flip_uv_v: BoolProperty(name="翻转 UV V 轴", default=False)

    def execute(self, context):
        try:
            imported = run_import(
                context,
                self.filepath,
                self.game,
                self.split_draw_calls,
                self.flip_winding,
                self.flip_uv_v,
            )
        except ReverseImportError as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        except Exception as exc:
            self.report({"ERROR"}, f"导入时发生未预期错误：{exc}")
            return {"CANCELLED"}

        self.report({"INFO"}, f"已导入 {imported} 个网格对象")
        return {"FINISHED"}


class REVERSE_PT_sidebar(Panel):
    bl_label = "反向导入器"
    bl_idname = "REVERSE_PT_sidebar"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "反向导入"

    def draw(self, context):
        layout = self.layout
        settings = context.scene.mod_reverse
        layout.prop(settings, "mod_path")
        layout.prop(settings, "game")
        layout.prop(settings, "split_draw_calls")
        layout.prop(settings, "flip_winding")
        layout.prop(settings, "flip_uv_v")

        op = layout.operator(REVERSE_OT_import.bl_idname, icon="IMPORT")
        op.filepath = settings.mod_path
        op.game = settings.game
        op.split_draw_calls = settings.split_draw_calls
        op.flip_winding = settings.flip_winding
        op.flip_uv_v = settings.flip_uv_v


def menu_func_import(self, context):
    self.layout.operator(
        IMPORT_SCENE_OT_mod_reverse.bl_idname,
        text="Mod 文件夹（.ini）",
    )


classes = (
    ReverseSettings,
    REVERSE_OT_import,
    IMPORT_SCENE_OT_mod_reverse,
    REVERSE_PT_sidebar,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.mod_reverse = bpy.props.PointerProperty(type=ReverseSettings)
    bpy.types.TOPBAR_MT_file_import.append(menu_func_import)


def unregister():
    bpy.types.TOPBAR_MT_file_import.remove(menu_func_import)
    del bpy.types.Scene.mod_reverse
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
