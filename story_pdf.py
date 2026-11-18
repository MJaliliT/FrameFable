"""Export the story, its preferences, and the ordered illustrations as a PDF."""

import base64
import binascii
import io
import re
from pathlib import Path
from xml.sax.saxutils import escape

IMAGE_URL = re.compile(r"data:image/(?:png|jpeg|webp);base64,([A-Za-z0-9+/=]+)")
UNIVERSAL_FONTS = (
    Path("/System/Library/Fonts/Supplemental/Arial Unicode.ttf"),
    Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
)


class StoryPdfError(ValueError):
    """An export error suitable for display."""


def clean_text(value):
    return "".join(character for character in value if character >= " " or character in "\r\n\t")


def story_data(data):
    if not isinstance(data, dict):
        raise StoryPdfError("Provide a story to export.")
    title, preferences, scenes = data.get("title"), data.get("preferences"), data.get("scenes")
    if not isinstance(title, str) or not title.strip() or len(title) > 500:
        raise StoryPdfError("The story needs a title before it can be exported.")
    if not isinstance(preferences, dict) or not isinstance(scenes, list) or not 1 <= len(scenes) <= 23:
        raise StoryPdfError("The story needs preferences and at least one scene.")
    for field in ("tone", "readingLevel"):
        if not isinstance(preferences.get(field), str) or len(preferences[field]) > 60:
            raise StoryPdfError("The story has an invalid reading level or tone.")
    for field in ("approximateWords", "sourceImages"):
        if type(preferences.get(field)) is not int or not 1 <= preferences[field] <= 10000:
            raise StoryPdfError("The story has an invalid length or image count.")
    if not all(isinstance(scene, dict) and isinstance(scene.get("text"), str)
               and 0 < len(scene["text"].strip()) <= 25_000 for scene in scenes):
        raise StoryPdfError("Every scene needs text before it can be exported.")
    created_at = data.get("createdAtLabel", "")
    if not isinstance(created_at, str) or len(created_at) > 120:
        raise StoryPdfError("The story has an invalid creation date.")
    return title.strip(), preferences, scenes, created_at.strip()


def image_bytes(url):
    if not isinstance(url, str):
        raise StoryPdfError("A story image is invalid.")
    match = IMAGE_URL.fullmatch(url)
    if not match:
        raise StoryPdfError("A story image is invalid.")
    try:
        return base64.b64decode(match[1], validate=True)
    except binascii.Error as error:
        raise StoryPdfError("A story image is invalid.") from error


def make_story_pdf(data):
    title, preferences, scenes, created_at = story_data(data)
    try:
        from PIL import Image as PillowImage, ImageOps
        from reportlab.lib import colors
        from reportlab.lib.enums import TA_CENTER
        from reportlab.lib.pagesizes import letter
        from reportlab.lib.styles import ParagraphStyle
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
        from reportlab.platypus import Image, KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
    except ImportError:
        raise StoryPdfError("Install the PDF dependency from requirements-pdf.txt and restart the panel.") from None

    ink = colors.HexColor("#39264c")
    muted = colors.HexColor("#675574")
    line = colors.HexColor("#ded2ed")
    content_width = letter[0] - 96
    title_font, body_font = "Times-Roman", "Times-Roman"
    try:
        (title + "".join(scene["text"] for scene in scenes)).encode("cp1252")
    except UnicodeEncodeError:
        for path in UNIVERSAL_FONTS:
            if path.is_file():
                try:
                    if "StoryUnicode" not in pdfmetrics.getRegisteredFontNames():
                        pdfmetrics.registerFont(TTFont("StoryUnicode", str(path)))
                except (OSError, ValueError):
                    continue
                title_font = body_font = "StoryUnicode"
                break

    title_style = ParagraphStyle("StoryTitle", fontName=title_font, fontSize=30, leading=34, textColor=ink)
    label_style = ParagraphStyle("StoryLabel", fontName="Helvetica", fontSize=9, leading=13, textColor=muted)
    value_style = ParagraphStyle("StoryValue", fontName="Helvetica", fontSize=11, leading=15, textColor=ink)
    body_style = ParagraphStyle("StoryText", fontName=body_font, fontSize=13, leading=19,
                                textColor=colors.HexColor("#292334"), spaceAfter=9)
    caption_style = ParagraphStyle("StoryImageCaption", parent=label_style, alignment=TA_CENTER,
                                   spaceBefore=5, spaceAfter=7)

    def paragraph(text, style):
        return Paragraph(escape(clean_text(text)).replace("\n", "<br/>"), style)

    labels = ["Tone", "Reading level", "Target length", "Source images"]
    values = [preferences["tone"], preferences["readingLevel"],
              f'About {preferences["approximateWords"]} words', str(preferences["sourceImages"])]
    heading = [paragraph(title, title_style)]
    if created_at:
        heading.extend([Spacer(1, 5), paragraph(f"Generated {created_at}", label_style)])
    table = Table([
        [heading, "", "", ""],
        [[paragraph(label, label_style), paragraph(value, value_style)] for label, value in zip(labels, values)],
    ], colWidths=[content_width / 4] * 4, hAlign="CENTER")
    table.setStyle(TableStyle([
        ("SPAN", (0, 0), (-1, 0)),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eee5fa")),
        ("BACKGROUND", (0, 1), (-1, 1), colors.HexColor("#f9f5fe")),
        ("BOX", (0, 0), (-1, -1), 0.6, line),
        ("INNERGRID", (0, 1), (-1, 1), 0.5, line),
        ("LINEABOVE", (0, 1), (-1, 1), 0.5, line),
        ("TOPPADDING", (0, 0), (-1, 0), 19),
        ("BOTTOMPADDING", (0, 0), (-1, 0), 17),
        ("TOPPADDING", (0, 1), (-1, 1), 8),
        ("BOTTOMPADDING", (0, 1), (-1, 1), 8),
        ("LEFTPADDING", (0, 0), (-1, -1), 12),
        ("RIGHTPADDING", (0, 0), (-1, -1), 10),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ]))

    def picture(url):
        try:
            with PillowImage.open(io.BytesIO(image_bytes(url))) as source:
                if source.width * source.height > 16_000_000:
                    raise StoryPdfError("A story image is too large to export.")
                image = ImageOps.exif_transpose(source).convert("RGBA")
                image.thumbnail((1200, 1200))
                flattened = PillowImage.new("RGB", image.size, "#ffffff")
                flattened.paste(image, mask=image.getchannel("A"))
        except (OSError, PillowImage.DecompressionBombError) as error:
            raise StoryPdfError("A story image could not be read.") from error
        encoded = io.BytesIO()
        flattened.save(encoded, format="JPEG", quality=90)
        encoded.seek(0)
        width, height = flattened.size
        ratio = min(content_width / width, 200 / height, 1.3)
        flowable = Image(encoded, width=width * ratio, height=height * ratio)
        flowable.hAlign = "CENTER"
        return flowable

    def footer(canvas, doc):
        canvas.setStrokeColor(line)
        canvas.line(48, 40, letter[0] - 48, 40)
        canvas.setFont("Helvetica", 9)
        canvas.setFillColor(muted)
        canvas.drawString(48, 25, "FrameFable")
        canvas.drawRightString(letter[0] - 48, 25, str(doc.page))

    flow = [table, Spacer(1, 22)]
    for scene in scenes:
        pieces = []
        if scene.get("image"):
            pieces.append(picture(scene["image"]))
        if scene.get("bridge"):
            pieces.append(paragraph("Connecting scene", caption_style))
        elif pieces:
            pieces.append(Spacer(1, 9))
        text = paragraph(scene["text"].strip(), body_style)
        if pieces:
            flow.append(KeepTogether([*pieces, text]))
        else:
            flow.append(text)
        flow.append(Spacer(1, 13))

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=letter, leftMargin=42, rightMargin=42, topMargin=44, bottomMargin=54,
                            title=clean_text(title), author="FrameFable")
    doc.build(flow, onFirstPage=footer, onLaterPages=footer)
    return buffer.getvalue()
