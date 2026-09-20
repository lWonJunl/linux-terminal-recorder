"""Lossless screenshot strips -> one streaming PNG and continuous image PDF."""

import math
import os
from pathlib import Path
import struct
import tempfile
import zlib

from PIL import Image


def validate_tiles(manifest: dict, directory: Path) -> list[Path]:
    if not isinstance(manifest, dict) or manifest.get("status") != "complete":
        raise ValueError("전체 스크롤 캡처가 완료되지 않았습니다.")
    if any(type(manifest.get(key)) is not int or manifest[key] < 1
           for key in ("width", "row_height", "total_rows")):
        raise ValueError("잘못된 파노라마 크기입니다.")
    if not isinstance(manifest.get("tiles"), list) or not manifest["tiles"]:
        raise ValueError("캡처 조각 목록이 없거나 잘못됐습니다.")
    width, row_height = manifest["width"], manifest["row_height"]
    directory = directory.resolve()
    next_row = 0
    paths = []
    for tile in manifest["tiles"]:
        if (not isinstance(tile, dict) or type(tile.get("first_row")) is not int
                or type(tile.get("rows")) is not int or tile["first_row"] != next_row or tile["rows"] < 1):
            raise ValueError("캡처에 누락되거나 중복된 줄이 있습니다.")
        filename = tile.get("file")
        if (not isinstance(filename, str) or not filename or filename in (".", "..")
                or any(char in filename for char in '/\\:\0') or Path(filename).name != filename):
            raise ValueError("잘못된 캡처 파일 경로입니다.")
        path = (directory / filename).resolve()
        if path.parent != directory:
            raise ValueError("캡처 파일 경로가 캡처 폴더를 벗어났습니다.")
        with Image.open(path) as image:
            if image.size != (width, tile["rows"] * row_height):
                raise ValueError("캡처 이미지와 줄 정보가 일치하지 않습니다.")
            image.verify()
        paths.append(path)
        next_row += tile["rows"]
    if next_row != manifest["total_rows"]:
        raise ValueError("마지막 줄까지 캡처되지 않았습니다.")
    return paths


def _png_chunk(stream, kind: bytes, data: bytes):
    stream.write(struct.pack("!I", len(data)))
    stream.write(kind)
    stream.write(data)
    stream.write(struct.pack("!I", zlib.crc32(kind + data) & 0xFFFFFFFF))


def write_long_png(path: Path, files: list[Path], width: int, height: int):
    """Write scanlines incrementally; never allocate a lecture-sized bitmap."""
    if max(width, height) >= 2**31:
        raise ValueError("PNG가 지원하는 이미지 크기를 초과했습니다.")
    compressor = zlib.compressobj(6)
    with path.open("xb") as stream:
        stream.write(b"\x89PNG\r\n\x1a\n")
        _png_chunk(stream, b"IHDR", struct.pack("!2I5B", width, height, 8, 2, 0, 0, 0))
        for source in files:
            with Image.open(source) as original:
                image = original.convert("RGB")
                data = image.tobytes()
                stride = width * 3
                for y in range(image.height):
                    encoded = compressor.compress(b"\0" + data[y * stride:(y + 1) * stride])
                    if encoded:
                        _png_chunk(stream, b"IDAT", encoded)
                image.close()
        tail = compressor.flush()
        if tail:
            _png_chunk(stream, b"IDAT", tail)
        _png_chunk(stream, b"IEND", b"")
        stream.flush()
        os.fsync(stream.fileno())


def write_pdf(path: Path, files: list[Path], manifest: dict, title: str) -> int:
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas

    width, line_height, total = manifest["width"], manifest["row_height"], manifest["total_rows"]
    scale = min(1.0, 720 / width)
    limit = max(1, int(14000 / (line_height * scale)))
    pages = math.ceil(total / limit)
    per_page = math.ceil(total / pages)
    pdf = canvas.Canvas(str(path))
    pdf.setTitle(title)
    global_row = page_row = 0
    page_rows = min(per_page, total)
    pdf.setPageSize((width * scale, page_rows * line_height * scale))
    for filename, tile in zip(files, manifest["tiles"]):
        offset = 0
        with Image.open(filename) as original:
            while offset < tile["rows"]:
                count = min(tile["rows"] - offset, page_rows - page_row)
                with original.crop((0, offset * line_height, width, (offset + count) * line_height)) as strip:
                    y = (page_rows - page_row - count) * line_height * scale
                    pdf.drawImage(ImageReader(strip), 0, y, width=width * scale,
                                  height=count * line_height * scale)
                offset += count
                global_row += count
                page_row += count
                if page_row == page_rows:
                    pdf.showPage()
                    page_row = 0
                    page_rows = min(per_page, total - global_row)
                    if page_rows:
                        pdf.setPageSize((width * scale, page_rows * line_height * scale))
    pdf.save()
    return pages


def export_panorama(directory: Path, state: dict, read_json, replace_file, publish_outputs) -> list[str]:
    manifest_path = Path(state["panorama_manifest"])
    manifest = read_json(manifest_path)
    files = validate_tiles(manifest, manifest_path.parent)
    height = manifest["total_rows"] * manifest["row_height"]
    basename = directory.name
    with tempfile.TemporaryDirectory(prefix=".export-", dir=directory) as temp:
        staging = Path(temp)
        png_name, pdf_name = basename + ".png", basename + ".pdf"
        write_long_png(staging / png_name, files, manifest["width"], height)
        pages = write_pdf(staging / pdf_name, files, manifest, state["name"])
        if state.get("output_directory"):
            outputs = publish_outputs([staging / png_name, staging / pdf_name],
                                      Path(state["output_directory"]), basename)
        else:
            # Preserve the saved location for sessions created by older versions.
            for name in (png_name, pdf_name):
                replace_file(staging / name, directory / name)
            outputs = [str(directory / png_name), str(directory / pdf_name)]
    state.update(panorama_width=manifest["width"], panorama_height=height, pdf_pages=pages)
    return outputs
