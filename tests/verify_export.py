"""Validate screenshot pixels and PDF image placement, optionally render QA views."""

import argparse
import json
from pathlib import Path
import re
import sys

from PIL import Image
from pypdf import PdfReader
from pypdf.generic import ContentStream

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from panorama_export import validate_tiles


def multiply(a, b):
    return (a[0]*b[0]+a[2]*b[1], a[1]*b[0]+a[3]*b[1],
            a[0]*b[2]+a[2]*b[3], a[1]*b[2]+a[3]*b[3],
            a[0]*b[4]+a[2]*b[5]+a[4], a[1]*b[4]+a[3]*b[5]+a[5])


def verify_panorama(state, reader, png_file):
    manifest_path = Path(state["panorama_manifest"])
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    files = validate_tiles(manifest, manifest_path.parent)
    with Image.open(png_file) as image:
        assert image.size == (manifest["width"], manifest["row_height"] * manifest["total_rows"])
        for tile, filename in zip(manifest["tiles"], files):
            top = tile["first_row"] * manifest["row_height"]
            with Image.open(filename) as original:
                with image.crop((0, top, image.width, top + original.height)) as section:
                    assert section.tobytes() == original.convert("RGB").tobytes()
        global_y = 0
        scale = min(1.0, 720 / image.width)
        for page in reader.pages:
            matrix, stack, page_y = (1, 0, 0, 1, 0, 0), [], 0
            page_height = float(page.mediabox.height)
            for operands, operation in ContentStream(page.get_contents(), reader).operations:
                if operation == b"q":
                    stack.append(matrix)
                elif operation == b"Q":
                    matrix = stack.pop()
                elif operation == b"cm":
                    matrix = multiply(matrix, tuple(float(v) for v in operands))
                elif operation == b"Do":
                    obj = page["/Resources"]["/XObject"][operands[0]].get_object()
                    width, height = int(obj["/Width"]), int(obj["/Height"])
                    assert width == image.width and height % manifest["row_height"] == 0
                    assert abs(matrix[0] - width * scale) < 0.002
                    assert abs(matrix[3] - height * scale) < 0.002
                    assert abs(matrix[4]) < 0.002
                    assert abs(matrix[5] - (page_height - (page_y + height) * scale)) < 0.002
                    with image.crop((0, global_y, image.width, global_y + height)) as section:
                        assert obj.get_data() == section.convert("RGB").tobytes()
                    global_y += height
                    page_y += height
            assert abs(page_y * scale - page_height) < 0.002
        assert global_y == image.height
    return manifest


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    parser.add_argument("--render", action="store_true")
    parser.add_argument("--expect-smoke", action="store_true")
    args = parser.parse_args()
    state = json.loads((args.directory / "session.json").read_text(encoding="utf-8"))
    pdf_file = next(Path(p) for p in state["outputs"] if p.endswith(".pdf"))
    pngs = [Path(p) for p in state["outputs"] if p.endswith(".png")]
    reader = PdfReader(pdf_file)
    manifest = None
    if state.get("mode") == "panorama-v1":
        assert len(pngs) == 1
        manifest = verify_panorama(state, reader, pngs[0])
    else:
        assert len(reader.pages) == len(pngs)
        for page, path in zip(reader.pages, pngs):
            with Image.open(path) as image:
                embedded = page.images[0].image.convert("RGB")
                assert image.size == embedded.size and image.convert("RGB").tobytes() == embedded.tobytes()
    if args.expect_smoke:
        rows = [row.rstrip() for row in (args.directory / "history.txt").read_text(encoding="utf-8").splitlines()]
        assert [row for row in rows if re.fullmatch(r"ROW-\d{4}", row)] == [f"ROW-{n:04d}" for n in range(1,601)]
        assert rows.count("REPEAT") == 2 and "COLOR" in rows and "테스트" in rows
        assert "PANORAMA-BEGIN" in rows and "PANORAMA-END" in rows
    if args.render:
        project = Path(__file__).resolve().parents[1]
        try:
            import pypdfium2 as pdfium
        except ModuleNotFoundError:
            parser.error("--render에는 pypdfium2가 필요합니다: python -m pip install pypdfium2")
        render_directory = project / "tmp" / "pdfs"
        render_directory.mkdir(parents=True, exist_ok=True)
        with pdfium.PdfDocument(pdf_file) as doc:
            for index in range(len(doc)):
                page = doc[index]
                bitmap = page.render(scale=1)
                path = render_directory / f"panorama-page-{index + 1}.png"
                bitmap.to_pil().save(path)
                bitmap.close()
                print(path)
                width, height = page.get_size()
                band = min(450, height)
                positions = [("head", 0), ("tail", max(0, height-band))]
                if index == 0 and manifest and len(manifest["tiles"]) > 1:
                    point = manifest["tiles"][0]["rows"] * manifest["row_height"] * min(1,720/manifest["width"])
                    positions.append(("seam", max(0, min(height-band, point-band/2))))
                for label, top in positions:
                    bitmap = page.render(scale=2, crop=(0, max(0,height-top-band), 0, top))
                    target = render_directory / f"panorama-page-{index + 1}-{label}.png"
                    bitmap.to_pil().save(target)
                    bitmap.close()
                    print(target)
                page.close()
    print(json.dumps({"result": "PASS", "screen_tiles": state["capture_count"],
                      "rows": state.get("panorama_rows"), "pages": len(reader.pages),
                      "pdf": str(pdf_file)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
