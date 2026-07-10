"""Markdown を日本語対応 PDF に変換する簡易コンバータ。

使い方:
    python md_to_pdf.py 入力.md 出力.pdf

reportlab 内蔵の日本語フォント（HeiseiKakuGo-W5）を使うため、
外部フォントのインストールは不要。見出し・箇条書き・表・水平線・
太字・インラインコードに対応。
"""

import re
import sys

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    HRFlowable,
    ListFlowable,
    ListItem,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

# 日本語フォントを埋め込む（コピー・検索可能な PDF にするため）。
# Windows の Yu Gothic を使い、失敗時は reportlab 内蔵フォントにフォールバック。
FONT = "JP"
FONT_BOLD = "JP-Bold"
try:
    pdfmetrics.registerFont(TTFont(FONT, r"C:\Windows\Fonts\YuGothR.ttc", subfontIndex=0))
    pdfmetrics.registerFont(TTFont(FONT_BOLD, r"C:\Windows\Fonts\YuGothB.ttc", subfontIndex=0))
    pdfmetrics.registerFontFamily(FONT, normal=FONT, bold=FONT_BOLD,
                                  italic=FONT, boldItalic=FONT_BOLD)
except Exception:
    FONT = FONT_BOLD = "HeiseiKakuGo-W5"
    pdfmetrics.registerFont(UnicodeCIDFont(FONT))

BASE = getSampleStyleSheet()


def style(name, **kw):
    return ParagraphStyle(name, parent=BASE["Normal"], fontName=FONT,
                          leading=kw.pop("leading", 16), **kw)


S = {
    "body": style("body", fontSize=10.5, leading=17, spaceAfter=4),
    "h1": style("h1", fontSize=20, leading=26, spaceBefore=6, spaceAfter=10,
                textColor=colors.HexColor("#1f3a63")),
    "h2": style("h2", fontSize=15, leading=21, spaceBefore=12, spaceAfter=6,
                textColor=colors.HexColor("#274b7d")),
    "h3": style("h3", fontSize=12.5, leading=18, spaceBefore=8, spaceAfter=4,
                textColor=colors.HexColor("#33598f")),
    "li": style("li", fontSize=10.5, leading=16),
    "cell": style("cell", fontSize=9, leading=13),
    "cellh": style("cellh", fontSize=9, leading=13, textColor=colors.white),
    "quote": style("quote", fontSize=10, leading=16, leftIndent=10,
                   textColor=colors.HexColor("#555555")),
}


def inline(text):
    """Markdown のインライン記法を reportlab のマークアップに変換。"""
    text = (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"`(.+?)`", r'<font backColor="#eef0f4">\1</font>', text)
    text = re.sub(r"\[(.+?)\]\((.+?)\)", r"\1", text)
    return text


def split_table_row(line):
    return [c.strip() for c in line.strip().strip("|").split("|")]


def build_table(rows):
    header = rows[0]
    body = rows[2:]  # rows[1] は区切り線
    ncols = len(header)
    data = [[Paragraph("<b>" + inline(c) + "</b>", S["cellh"]) for c in header]]
    for r in body:
        cells = (r + [""] * ncols)[:ncols]
        data.append([Paragraph(inline(c), S["cell"]) for c in cells])

    avail = A4[0] - 40 * mm
    col_w = avail / ncols
    t = Table(data, colWidths=[col_w] * ncols, repeatRows=1)
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#274b7d")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1),
         [colors.white, colors.HexColor("#f4f6fa")]),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#b7c3d6")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    return t


def parse(md):
    flow = []
    lines = md.split("\n")
    i = 0
    bullets = []

    def flush_bullets():
        nonlocal bullets
        if bullets:
            items = [ListItem(Paragraph(inline(b), S["li"]), leftIndent=12)
                     for b in bullets]
            flow.append(ListFlowable(items, bulletType="bullet",
                                     start="•", leftIndent=14))
            flow.append(Spacer(1, 4))
            bullets = []

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        if stripped.startswith("|") and i + 1 < len(lines) and \
                re.match(r"^\|?[\s:|-]+\|?$", lines[i + 1].strip()):
            flush_bullets()
            rows = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                rows.append(split_table_row(lines[i]))
                i += 1
            flow.append(build_table(rows))
            flow.append(Spacer(1, 8))
            continue

        if not stripped:
            flush_bullets()
            i += 1
            continue

        if stripped.startswith("### "):
            flush_bullets()
            flow.append(Paragraph(inline(stripped[4:]), S["h3"]))
        elif stripped.startswith("## "):
            flush_bullets()
            flow.append(Paragraph(inline(stripped[3:]), S["h2"]))
        elif stripped.startswith("# "):
            flush_bullets()
            flow.append(Paragraph(inline(stripped[2:]), S["h1"]))
        elif stripped in ("---", "***", "___"):
            flush_bullets()
            flow.append(Spacer(1, 2))
            flow.append(HRFlowable(width="100%", thickness=0.6,
                                   color=colors.HexColor("#c2ccdb")))
            flow.append(Spacer(1, 6))
        elif stripped.startswith("> "):
            flush_bullets()
            flow.append(Paragraph(inline(stripped[2:]), S["quote"]))
        elif re.match(r"^[-*]\s+", stripped):
            bullets.append(re.sub(r"^[-*]\s+", "", stripped))
        else:
            flush_bullets()
            flow.append(Paragraph(inline(stripped), S["body"]))
        i += 1

    flush_bullets()
    return flow


def convert(src, dst):
    with open(src, encoding="utf-8") as f:
        md = f.read()
    doc = SimpleDocTemplate(dst, pagesize=A4,
                            leftMargin=20 * mm, rightMargin=20 * mm,
                            topMargin=18 * mm, bottomMargin=18 * mm,
                            title=src)
    doc.build(parse(md))
    print(f"生成: {dst}")


if __name__ == "__main__":
    convert(sys.argv[1], sys.argv[2])
