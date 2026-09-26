"""Render OCSR bounding boxes and drawn-vs-parsed comparison grids."""

from __future__ import annotations

import re
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
from rdkit import Chem
from rdkit.Chem import Draw


FONT_PATHS = (
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
)


def _font(size: int):
    for path in FONT_PATHS:
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def _score_color(score):
    try:
        value = float(score)
    except Exception:
        value = 0.0
    if value >= 0.9:
        return 54, 200, 113
    if value >= 0.5:
        return 230, 162, 60
    return 240, 97, 109


def annotate_detections(fig_png: str, detections, out_png: str):
    image = Image.open(fig_png).convert("RGB")
    width, _ = image.size
    draw = ImageDraw.Draw(image)
    line_width = max(2, width // 500)
    font_size = max(15, width // 55)
    label_font = _font(font_size)

    for detection in detections or []:
        bbox = detection.get("bbox") if isinstance(detection, dict) else None
        if not (isinstance(bbox, (list, tuple)) and len(bbox) == 4):
            continue
        x0, y0, x1, y1 = (int(value) for value in bbox)
        color = _score_color(detection.get("score"))
        draw.rectangle((x0, y0, x1, y1), outline=color, width=line_width)
        label = str(detection.get("bbox_index", "?"))
        left, top, right, bottom = draw.textbbox((0, 0), label, font=label_font)
        text_width, text_height = right - left, bottom - top
        pad = max(3, font_size // 5)
        chip_x, chip_y = x0, max(0, y0 - text_height - 2 * pad)
        draw.rectangle(
            (chip_x, chip_y, chip_x + text_width + 2 * pad, chip_y + text_height + 2 * pad),
            fill=color,
        )
        draw.text((chip_x + pad, chip_y + pad), label, fill=(0, 0, 0), font=label_font)

    image.save(out_png, "PNG")
    return str(Path(out_png).resolve())


def render_detections_grid(detections, fig_png: str, out_png: str, sub=(210, 180)):
    items = [item for item in (detections or []) if isinstance(item, dict)]
    if not items:
        return None

    figure = Image.open(fig_png).convert("RGB")
    figure_width, figure_height = figure.size
    sub_width, sub_height = sub
    header_height, caption_height, gap = 28, 18, 6
    cell_width = sub_width * 2 + gap
    cell_height = header_height + sub_height + caption_height
    per_row = 1 if len(items) <= 3 else 2
    rows = (len(items) + per_row - 1) // per_row
    canvas = Image.new("RGB", (per_row * cell_width, rows * cell_height), "white")
    draw = ImageDraw.Draw(canvas)
    header_font, small_font = _font(16), _font(12)

    def fit(image):
        tile = Image.new("RGB", (sub_width, sub_height), "white")
        thumb = image.copy()
        thumb.thumbnail((sub_width - 4, sub_height - 4))
        tile.paste(thumb, ((sub_width - thumb.width) // 2, (sub_height - thumb.height) // 2))
        return tile

    def render_smiles(smiles):
        molecule = Chem.MolFromSmiles(smiles) if smiles else None
        if molecule is None:
            return None
        return Draw.MolToImage(molecule, size=(sub_width, sub_height))

    def esmiles_body(value):
        value = re.sub(r"<a>[^<]*</a>|<r>[^<]*</r>", "", value or "")
        return value.replace("<sep>", ".").replace("<dum>", "*").strip(". ")

    for index, detection in enumerate(items):
        row, column = divmod(index, per_row)
        origin_x, origin_y = column * cell_width, row * cell_height
        bbox_index = detection.get("bbox_index", index)
        color = _score_color(detection.get("score"))
        try:
            score = f"{float(detection.get('score')):.2f}"
        except Exception:
            score = "?"

        parsed_esmi = (detection.get("esmi") or "").strip()
        renderable_esmi = esmiles_body(parsed_esmi)

        draw.rectangle(
            (origin_x, origin_y, origin_x + cell_width, origin_y + header_height),
            fill=color,
        )
        heading = f"#{bbox_index}   OCSR {score}"
        draw.text((origin_x + 6, origin_y + 5), heading, fill=(0, 0, 0), font=header_font)

        bbox = detection.get("bbox")
        if isinstance(bbox, (list, tuple)) and len(bbox) == 4:
            x0, y0, x1, y1 = bbox
            pad_x, pad_y = (x1 - x0) * 0.06, (y1 - y0) * 0.06
            crop = figure.crop(
                (
                    max(0, int(x0 - pad_x)),
                    max(0, int(y0 - pad_y)),
                    min(figure_width, int(x1 + pad_x)),
                    min(figure_height, int(y1 + pad_y)),
                )
            )
            canvas.paste(fit(crop), (origin_x, origin_y + header_height))

        pane_x = origin_x + sub_width + gap
        rendered = render_smiles(renderable_esmi)
        if rendered is not None:
            canvas.paste(rendered, (pane_x, origin_y + header_height))
        else:
            draw.rectangle(
                (
                    pane_x,
                    origin_y + header_height,
                    pane_x + sub_width,
                    origin_y + header_height + sub_height,
                ),
                outline=(220, 220, 220),
            )
            draw.text(
                (pane_x + sub_width // 2 - 22, origin_y + header_height + sub_height // 2 - 7),
                "markush" if "*" in renderable_esmi else "—",
                fill=(150, 150, 150),
                font=small_font,
            )

        draw.text(
            (origin_x + 6, origin_y + header_height + sub_height + 2),
            "drawn (figure)",
            fill=(90, 90, 90),
            font=small_font,
        )
        draw.text(
            (origin_x + sub_width + gap + 4, origin_y + header_height + sub_height + 2),
            "OCSR esmi (verbatim)",
            fill=(90, 90, 90),
            font=small_font,
        )

    canvas.save(out_png, "PNG")
    return str(Path(out_png).resolve())
