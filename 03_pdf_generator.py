"""สร้าง PDF วิถีจรชะตา ทำนายด้วย ReportLab.

ทดลองในเครื่อง: python 03_pdf_generator.py --demo output.pdf
วาง Sarabun.ttf หรือ Sarabun-Regular.ttf ข้างไฟล์นี้ หรือกำหนด --font
และใช้ Sarabun-Bold.ttf เป็นตัวหนาได้หากมีไฟล์
"""
from __future__ import annotations

from argparse import ArgumentParser
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from html import escape
from io import BytesIO
from itertools import groupby
from os import environ
from pathlib import Path
from threading import RLock
from typing import Mapping, Sequence
from unicodedata import normalize
from zoneinfo import ZoneInfo
import re

from PIL import Image as PILImage, ImageOps, UnidentifiedImageError
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    Image, KeepTogether, LongTable, Paragraph, SimpleDocTemplate, Spacer, TableStyle,
)

BANGKOK = ZoneInfo("Asia/Bangkok")
SIGNATURE_LABEL = "รหัสลายเซ็นกำกับดวงเฉพาะบุคคล:"
COPYRIGHT_LINE = "© สงวนลิขสิทธิ์โดย วิถีจรชะตา ทำนาย (นิธิวิทย์ ภัทรวิมลวรรณ และ รัฏฐพิชญ์ แสงดา)"
RIGHTS_NOTICE = (
    "คำสงวนสิทธิ์: เอกสารและผังพยากรณ์ฉบับนี้จัดทำขึ้นเฉพาะบุคคล "
    "ห้ามคัดลอก ดัดแปลง ทำซ้ำ เผยแพร่ต่อสาธารณะ "
    "หรือนำไปใช้ประมวลผล/ฝึกสอนระบบปัญญาประดิษฐ์ "
    "(No AI Training / No Scraping Prohibited) โดยไม่ได้รับอนุญาตเป็นลายลักษณ์อักษร"
)
COPYRIGHT = f"{COPYRIGHT_LINE}\n{RIGHTS_NOTICE}"
BASE = "พื้นดวงและประเด็นสำคัญ"
QUESTIONS = "คำตอบเฉพาะบุคคล"
PLAN = "แผนปฏิบัติ 7 วัน"
D6 = "คำพยากรณ์ 6 เดือน"
Y12 = "คำพยากรณ์ 12 เดือน"
WINDOWS = "ตารางเวลาทอง 7 วัน"
FOCUS = "คำวิเคราะห์ประเด็นคาใจ"
D30 = "พยากรณ์รายวัน 30 วัน"
PAIR = "ดวงสมพงศ์ของทั้งสองคน"
D90 = "จังหวะความสัมพันธ์ 90 วัน"
WALLPAPER = "สีและเลขมงคล"
CONSULT = "หัวข้อปรึกษา 30 นาที"
IMAGE_LABELS = {"chart": "ผังดวงจักรราศี", "yantra": "ยันต์พิชัยสงคราม", "wallpaper": "วอลเปเปอร์มงคล 9:16",
                "personal_yantra": "ยันต์เสริมดวงเฉพาะบุคคล"}
MAX_IMAGE_BYTES = 6_000_000
MAX_PDF_BYTES = 12_000_000
MAX_PAGES = 120
BUILD_LOCK = RLock()
INK = colors.HexColor("#243047")
ACCENT = colors.HexColor("#443468")
GOLD = colors.HexColor("#A9894D")
PALE = colors.HexColor("#EFEBF5")


@dataclass(frozen=True)
class PackageSpec:
    sections: Sequence[str]
    images: Sequence[str] = ()
    question_count: int = 0
    chat: bool = False


PACKAGES = {
    "SM-99": PackageSpec((BASE, QUESTIONS), question_count=1, chat=True),
    "SM-149": PackageSpec((WALLPAPER,), ("wallpaper",)),
    "SM-249": PackageSpec((BASE, QUESTIONS, PLAN), ("chart",), 3),
    "SM-499": PackageSpec((D6, QUESTIONS), ("chart", "yantra"), 5),
    "FW-250": PackageSpec((BASE, QUESTIONS), question_count=2),
    "FW-490": PackageSpec((D6, QUESTIONS), ("chart", "yantra"), 5),
    "FW-890": PackageSpec((Y12, WINDOWS, CONSULT), ("chart", "yantra")),
    "LG-690": PackageSpec((FOCUS,), ("chart", "yantra"), 1),
    "LG-990": PackageSpec((FOCUS, WINDOWS), ("chart", "yantra")),
    "LG-1490": PackageSpec((BASE, D30), ("chart", "yantra")),
    "LG-2190": PackageSpec((PAIR,), ("chart", "yantra")),
    "LG-2990": PackageSpec((PAIR, D90), ("chart", "yantra")),
}


def find_font(regular: str | Path | None = None, bold: str | Path | None = None) -> tuple[Path, Path]:
    here = Path(__file__).resolve().parent
    windows = Path(environ.get("WINDIR", "C:/Windows")) / "Fonts"
    candidates = (
        here / "Sarabun.ttf",
        here / "Sarabun-Regular.ttf",
        here / "fonts" / "Sarabun-Regular.ttf",
        Path("/usr/share/fonts/truetype/noto/NotoSansThai-Regular.ttf"),
        Path("/System/Library/Fonts/Supplemental/Thonburi.ttf"),
        windows / "tahoma.ttf",
    )
    primary = Path(regular).expanduser() if regular else next((item for item in candidates if item.is_file()), None)
    if primary is None or not primary.is_file():
        raise ValueError("วาง Sarabun.ttf หรือ Sarabun-Regular.ttf ข้าง 03_pdf_generator.py หรือตั้ง FONT_REGULAR")
    if bold:
        strong = Path(bold).expanduser()
        if not strong.is_file():
            raise ValueError("ไฟล์ FONT_BOLD ที่ระบุไม่พบ")
    else:
        alternatives = (
            primary.with_name("Sarabun-Bold.ttf"),
            primary.with_name("NotoSansThai-Bold.ttf"),
            primary.with_name("tahomabd.ttf"),
        )
        strong = next((item for item in alternatives if item.is_file()), primary)
    return primary, strong


def register_font(path: Path) -> str:
    try:
        font_data = path.read_bytes()
        name = "WichiThai-" + sha256(font_data).hexdigest()[:24]
        if name not in pdfmetrics.getRegisteredFontNames():
            font = TTFont(name, BytesIO(font_data), shapable=True)
            if ord("ก") not in font.face.charToGlyph or ord("้") not in font.face.charToGlyph:
                raise ValueError("ไฟล์ฟอนต์ที่เลือกไม่มีอักขระไทย")
            pdfmetrics.registerFont(font)
    except (OSError, KeyError, TypeError, IndexError) as exc:
        raise ValueError("อ่านไฟล์ฟอนต์ไทยไม่ได้") from exc
    return name


def signature_code(order_code: str, issued_at: datetime) -> str:
    if not order_code.strip() or issued_at.utcoffset() is None:
        raise ValueError("ต้องระบุรหัสออเดอร์และเวลาแบบมี timezone")
    code = sha256(order_code.strip().encode("utf-8")).hexdigest()[:12].upper()
    return f"SIG-WJCT-{issued_at.astimezone(BANGKOK):%Y%m%d}-{code}"


def clean_text(value: object) -> str:
    text = "" if value is None else str(value)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return normalize("NFC", re.sub(r"[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]", "", text)).strip()


def pipe_rows(value: str, count: int) -> list[Sequence[str]]:
    rows = [tuple(cell.strip() for cell in line.split("|")) for line in value.splitlines() if line.strip()]
    if not rows or any(len(row) != count or not all(row) for row in rows):
        raise ValueError("ตรวจจำนวนช่องของข้อมูลตารางและกรอกทุกช่อง")
    if any(len(cell) > 4000 for row in rows for cell in row):
        raise ValueError("ข้อมูลตารางแต่ละช่องใช้ได้ไม่เกิน 4,000 อักขระ")
    return rows


def golden_window_rows(value: str) -> list[Sequence[str]]:
    rows = pipe_rows(value, 3)
    if len(rows) != 7 or len({row[0] for row in rows}) != 7:
        raise ValueError("ตารางเวลาทองใช้ 7 วันที่ต่างกัน รูปแบบ วันที่ | ช่วงเวลา | คำแนะนำ")
    return rows


def pair_rows(order: Mapping[str, object], analysis: str):
    partner = order.get("partner_birth_data")
    if not isinstance(partner, Mapping) or partner.get("consent") is not True:
        raise ValueError("รายงานดวงคู่ต้องมีข้อมูลและความยินยอมของคนที่ 2")
    client_name = clean_text(order.get("client_name")) or "คนที่ 1"
    partner_name = clean_text(partner.get("name"))
    if not partner_name or not clean_text(partner.get("birth_date")) or not clean_text(partner.get("birth_province")):
        raise ValueError("ข้อมูลคนที่ 2 ต้องมีชื่อ วันเกิด และสถานที่เกิด")
    if "|" in analysis:
        rows = pipe_rows(analysis, 4)
        headings = ("มิติสมพงศ์", client_name, partner_name, "ข้อสรุปร่วม")
        return headings, rows, ""
    rows = [
        ("วันเกิด", clean_text(order.get("birth_date")) or "ไม่ระบุ", clean_text(partner.get("birth_date"))),
        ("เวลาเกิด", clean_text(order.get("birth_time")) or "ไม่ทราบ", clean_text(partner.get("birth_time")) or "ไม่ทราบ"),
        ("สถานที่เกิด", clean_text(order.get("birth_province")) or "ไม่ระบุ", clean_text(partner.get("birth_province"))),
    ]
    return ("ข้อมูลที่ใช้เทียบดวง", client_name, partner_name), rows, analysis


def prepared_image(data: bytes, key: str) -> tuple[bytes, int, int]:
    if not data or len(data) > MAX_IMAGE_BYTES:
        raise ValueError("ภาพประกอบต้องมีขนาดไม่เกิน 6 MB ต่อไฟล์")
    try:
        with PILImage.open(BytesIO(data)) as source:
            width, height = source.size
            if source.format not in ("PNG", "JPEG") or width * height > 20_000_000:
                raise ValueError("ใช้ PNG หรือ JPEG ขนาดไม่เกิน 20 ล้านพิกเซล")
            if key == "wallpaper" and (source.format != "PNG" or width * 16 != height * 9):
                raise ValueError("วอลเปเปอร์ต้องเป็น PNG อัตราส่วน 9:16")
            source.verify()
        with PILImage.open(BytesIO(data)) as source:
            oriented = ImageOps.exif_transpose(source)
            if oriented.mode in ("RGBA", "LA") or "transparency" in oriented.info:
                rgba = oriented.convert("RGBA")
                page_image = PILImage.new("RGB", rgba.size, "white")
                page_image.paste(rgba, mask=rgba.getchannel("A"))
            else:
                page_image = oriented.convert("RGB")
            page_image.thumbnail((1800, 2200))
            buffer = BytesIO()
            page_image.save(buffer, format="PNG", optimize=True)
            return buffer.getvalue(), page_image.width, page_image.height
    except (UnidentifiedImageError, OSError, PILImage.DecompressionBombError) as exc:
        raise ValueError("เปิดไฟล์ภาพประกอบไม่ได้") from exc


def validate_report(package: str, sections: Mapping[str, str], images: Mapping[str, bytes]) -> PackageSpec:
    spec = PACKAGES.get(package)
    if spec is None:
        raise ValueError("รหัสแพ็กเกจไม่อยู่ในชุดแม่แบบ")
    missing = [name for name in spec.sections if not clean_text(sections.get(name))]
    if missing:
        raise ValueError("กรอกเนื้อหาให้ครบ " + ", ".join(missing))
    if sum(len(str(sections.get(name, ""))) for name in spec.sections) > 120_000:
        raise ValueError("เนื้อหารายงานเกิน 120,000 อักขระ")
    if WINDOWS in spec.sections:
        golden_window_rows(sections[WINDOWS])
    if package == "SM-149" and images.get("personal_yantra"):
        if images.get("wallpaper"):
            raise ValueError("เลือกวอลเปเปอร์หรือยันต์เสริมดวงสำหรับ SM-149 หนึ่งแบบ")
        spec = PackageSpec(spec.sections, ("personal_yantra",))
    absent = [IMAGE_LABELS[key] for key in spec.images if not images.get(key)]
    if absent:
        raise ValueError("แนบภาพที่ตรวจแล้ว " + ", ".join(absent))
    return spec


def paragraph(value, style):
    primary = pdfmetrics.getFont(style.fontName).face.charToGlyph

    def required_font(character):
        return style.fontName if primary.get(ord(character), 0) or character.isspace() else style.latinFont

    lines = []
    for line in clean_text(value).split("\n"):
        fragments = []
        for name, run in groupby(line, required_font):
            markup = escape("".join(run))
            fragments.append(markup if name == style.fontName else f'<font name="{name}">{markup}</font>')
        lines.append("".join(fragments))
    return Paragraph("<br/>".join(lines), style)


def styles_for(regular: str, bold: str):
    base = ParagraphStyle(
        "WichiBody", fontName=regular, fontSize=11.7, leading=17.2,
        textColor=INK, alignment=TA_LEFT, spaceAfter=8, shaping=1, splitLongWords=True, latinFont='Helvetica',
    )
    return {
        "body": base,
        "title": ParagraphStyle("WichiTitle", parent=base, fontName=bold, latinFont="Helvetica-Bold", fontSize=20, leading=28, textColor=ACCENT),
        "section": ParagraphStyle("WichiSection", parent=base, fontName=bold, latinFont="Helvetica-Bold", fontSize=14, leading=20,
                                  textColor=ACCENT, spaceBefore=8, keepWithNext=True),
        "meta": ParagraphStyle("WichiMeta", parent=base, fontSize=10, leading=15, textColor=INK),
        "cell": ParagraphStyle("WichiCell", parent=base, fontSize=9.4, leading=14, spaceAfter=0),
        "cell_head": ParagraphStyle("WichiCellHead", parent=base, fontName=bold, latinFont="Helvetica-Bold", fontSize=9.4,
                                    leading=14, spaceAfter=0, textColor=ACCENT),
        "footer": ParagraphStyle("WichiFooter", parent=base, fontSize=8.4, leading=11, spaceAfter=0),
        "footer_id": ParagraphStyle("WichiFooterID", parent=base, fontName=bold, latinFont="Helvetica-Bold", fontSize=8.7,
                                    leading=12, spaceAfter=0),
    }


def report_table(titles, rows, fractions, width, styles):
    body = [[paragraph(text, styles["cell_head"]) for text in titles]]
    body.extend([paragraph(text, styles["cell"]) for text in row] for row in rows)
    table = LongTable(body, colWidths=[ratio * width for ratio in fractions],
                      repeatRows=1, splitByRow=1, splitInRow=1, hAlign="LEFT")
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), PALE),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#FAF9FC")]),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#D7CFDE")),
        ("LEFTPADDING", (0, 0), (-1, -1), 7),
        ("RIGHTPADDING", (0, 0), (-1, -1), 7),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))
    return table


def report_story(order, sections, images, spec, issued_at, width, styles):
    story = [
        paragraph("รายงานคำพยากรณ์เฉพาะบุคคล", styles["title"]),
        paragraph(f"{clean_text(order.get('package_name')) or order['package_tier']}\n"
                  f"จัดทำให้ {clean_text(order.get('client_name')) or 'ผู้รับรายงาน'}\n"
                  f"วันที่ออกเอกสาร {issued_at.astimezone(BANGKOK):%d/%m/%Y} ตามเวลาไทย", styles["meta"]),
        Spacer(1, 6),
    ]
    for title in spec.sections:
        text = clean_text(sections[title])
        story.append(paragraph(title, styles["section"]))
        if title == WINDOWS:
            story.append(report_table(
                ("วันที่", "ช่วงเวลาไทย", "คำแนะนำ"), golden_window_rows(text),
                (.23, .25, .52), width, styles,
            ))
        elif title == PAIR:
            headings, rows, note = pair_rows(order, text)
            ratios = (.22, .24, .24, .30) if len(headings) == 4 else (.31, .345, .345)
            story.append(report_table(headings, rows, ratios, width, styles))
            if note:
                story.extend((Spacer(1, 8), paragraph(note, styles["body"])))
        else:
            for paragraph_text in text.split("\n\n"):
                lines = paragraph_text.splitlines()
                for index in range(0, len(lines), 5):
                    story.append(paragraph("\n".join(lines[index:index + 5]), styles["body"]))
        story.append(Spacer(1, 8))
    for key in spec.images:
        data, img_width, img_height = prepared_image(images[key], key)
        factor = min(width / img_width, 440 / img_height)
        drawing = Image(BytesIO(data), img_width * factor, img_height * factor, hAlign="CENTER")
        story.append(KeepTogether([paragraph(IMAGE_LABELS[key], styles["section"]), drawing, Spacer(1, 10)]))
    return story


def render_pdf(
    order: Mapping[str, object],
    sections: Mapping[str, str],
    images: Mapping[str, bytes],
    issued_at: datetime,
    font_regular: str | Path | None = None,
    font_bold: str | Path | None = None,
) -> tuple[bytes, str]:
    code = str(order.get("order_code") or "")
    stamp = signature_code(code, issued_at)
    spec = validate_report(str(order.get("package_tier") or ""), sections, images)
    font_files = find_font(font_regular, font_bold)
    with BUILD_LOCK:
        regular, bold = (register_font(path) for path in font_files)
        styles = styles_for(regular, bold)
        width, page_height = A4
        margin = 52
        body_width = width - 2 * margin - 12
        footer_parts = [
            paragraph(f"{SIGNATURE_LABEL} {stamp}", styles["footer_id"]),
            paragraph(COPYRIGHT_LINE, styles["footer"]),
            paragraph(RIGHTS_NOTICE, styles["footer"]),
        ]
        heights = [element.wrap(body_width, page_height)[1] for element in footer_parts]
        bottom_margin = 44 + sum(heights) + 6
        output = BytesIO()
        doc = SimpleDocTemplate(output, pagesize=A4, leftMargin=margin, rightMargin=margin,
                                topMargin=66, bottomMargin=bottom_margin, pageCompression=1,
                                title="รายงานวิถีจรชะตา ทำนาย", author="วิถีจรชะตา ทำนาย",
                                subject=stamp)

        def page_details(canvas, page_doc):
            if page_doc.page > MAX_PAGES:
                raise ValueError("จำนวนหน้าเกิน 120 หน้า")
            canvas.saveState()
            canvas.setStrokeColor(GOLD)
            canvas.setLineWidth(0.65)
            canvas.line(margin, page_height - 50, width - margin, page_height - 50)
            canvas.setFont(bold, 11)
            canvas.setFillColor(ACCENT)
            canvas.drawString(margin + 6, page_height - 40, "วิถีจรชะตา ทำนาย", shaping=True)
            canvas.setStrokeColor(GOLD)
            canvas.line(margin, bottom_margin - 4, width - margin, bottom_margin - 4)
            cursor = bottom_margin - 12
            for element, height in zip(footer_parts, heights):
                cursor -= height
                element.drawOn(canvas, margin + 6, cursor)
                cursor -= 2
            canvas.setFont("Helvetica", 8)
            canvas.setFillColor(INK)
            canvas.drawRightString(width - margin, 23, str(page_doc.page))
            canvas.restoreState()

        doc.build(report_story(order, sections, images, spec, issued_at, body_width, styles),
                  onFirstPage=page_details, onLaterPages=page_details)
        result = output.getvalue()
    if len(result) > MAX_PDF_BYTES:
        raise ValueError("รายงาน PDF เกิน 12 MB")
    return result, stamp


def chat_text(order: Mapping[str, object], sections: Mapping[str, str], stamp: str) -> str:
    spec = PACKAGES[str(order["package_tier"])]
    return "\n\n".join(
        ["วิถีจรชะตา ทำนาย", "ออเดอร์ " + str(order["order_code"])]
        + [label + "\n" + clean_text(sections[label]) for label in spec.sections]
        + [f"{SIGNATURE_LABEL} {stamp}", COPYRIGHT]
    )


def create_demo(output: Path, font_regular: Path | None = None):
    order = {
        "order_code": "WJ-DEMO-LOCAL", "package_tier": "SM-99",
        "package_name": "Social แพ็กเกจเปิดใจ", "client_name": "ผู้รับรายงานทดสอบ",
    }
    sections = {
        BASE: "เอกสารทดสอบการแสดงผลภาษาไทยสำหรับระบบวิถีจรชะตา ทำนาย",
        QUESTIONS: "ตัวอย่างการจัดย่อหน้าและวรรณยุกต์ ใช้เพื่อทดสอบการออกไฟล์จากเครื่อง",
    }
    data, stamp = render_pdf(order, sections, {}, datetime.now(timezone.utc), font_regular)
    output.write_bytes(data)
    return stamp


if __name__ == "__main__":
    parser = ArgumentParser(description="ทดลองสร้าง PDF จากข้อมูลสมมติ")
    parser.add_argument("--demo", required=True, type=Path, help="ตำแหน่งเขียนไฟล์ PDF")
    parser.add_argument("--font", type=Path, help="ไฟล์ Sarabun.ttf หรือฟอนต์ไทย TTF")
    args = parser.parse_args()
    demo_stamp = create_demo(args.demo, args.font)
    print(f"{args.demo}: {demo_stamp}")
