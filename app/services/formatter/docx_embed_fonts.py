"""Embed Libre Baskerville (regular + bold) into a .docx package (OOXML obfuscated font parts)."""

from __future__ import annotations

import io
import logging
import uuid
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

from app.services.formatter.book_typography import (
    LIBRE_BASKERVILLE,
    LIBRE_BASKERVILLE_BOLD_TTF,
    LIBRE_BASKERVILLE_REGULAR_TTF,
    font_files_present,
)

logger = logging.getLogger(__name__)

_CT_NS = "http://schemas.openxmlformats.org/package/2006/content-types"


def _obfuscate_font(plain: bytes, guid: uuid.UUID) -> bytes:
    key_bytes = bytes.fromhex(guid.hex)
    double_key_int = int.from_bytes(key_bytes * 2, byteorder="big", signed=False)
    plain_int = int.from_bytes(plain[:32], byteorder="little", signed=False)
    obf_int = plain_int ^ double_key_int
    obf32 = obf_int.to_bytes(32, byteorder="little", signed=False)
    return obf32 + plain[32:]


def _font_table_rels_xml() -> bytes:
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/font" '
        'Target="fonts/font1.odttf"/>'
        '<Relationship Id="rId2" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/font" '
        'Target="fonts/font2.odttf"/>'
        "</Relationships>"
    ).encode("utf-8")


def _patch_font_table_xml(xml_bytes: bytes, g_reg: uuid.UUID, g_bold: uuid.UUID) -> bytes:
    text = xml_bytes.decode("utf-8")
    if LIBRE_BASKERVILLE in text and "embedRegular" in text and "embedBold" in text:
        return xml_bytes
    insert = (
        f'  <w:font w:name="{LIBRE_BASKERVILLE}">\n'
        f'    <w:embedRegular r:id="rId1" w:fontKey="{{{str(g_reg).upper()}}}"/>\n'
        f'    <w:embedBold r:id="rId2" w:fontKey="{{{str(g_bold).upper()}}}"/>\n'
        "  </w:font>\n"
    )
    if "</w:fonts>" not in text:
        raise ValueError("fontTable.xml: missing </w:fonts>")
    return text.replace("</w:fonts>", insert + "</w:fonts>", 1).encode("utf-8")


def _ensure_odttf_content_type(ct_bytes: bytes) -> bytes:
    if b'Extension="odttf"' in ct_bytes or b"Extension='odttf'" in ct_bytes:
        return ct_bytes
    root = ET.fromstring(ct_bytes)
    d = ET.Element(f"{{{_CT_NS}}}Default")
    d.set("Extension", "odttf")
    d.set(
        "ContentType",
        "application/vnd.openxmlformats-officedocument.obfuscatedFont",
    )
    root.insert(0, d)
    buf = io.BytesIO()
    ET.ElementTree(root).write(buf, encoding="UTF-8", xml_declaration=True)
    return buf.getvalue()


def embed_libre_baskerville(docx_path: Path) -> None:
    """Patch ``docx_path`` in place to embed bundled Libre Baskerville TTFs."""
    if not font_files_present():
        logger.warning(
            "Libre Baskerville font files missing under app/assets/fonts; "
            "skipping DOCX embedding."
        )
        return

    path = docx_path.resolve()
    regular = LIBRE_BASKERVILLE_REGULAR_TTF.read_bytes()
    bold = LIBRE_BASKERVILLE_BOLD_TTF.read_bytes()
    g_reg = uuid.uuid4()
    g_bold = uuid.uuid4()
    o_reg = _obfuscate_font(regular, g_reg)
    o_bold = _obfuscate_font(bold, g_bold)

    with zipfile.ZipFile(path, "r") as zin:
        parts = {n: zin.read(n) for n in zin.namelist()}

    ft_xml = parts.get("word/fontTable.xml")
    if not ft_xml:
        logger.warning("word/fontTable.xml missing; skipping font embedding.")
        return

    try:
        parts["word/fontTable.xml"] = _patch_font_table_xml(ft_xml, g_reg, g_bold)
    except ValueError as e:
        logger.warning("Font table patch failed: %s", e)
        return

    parts["word/_rels/fontTable.xml.rels"] = _font_table_rels_xml()
    parts["word/fonts/font1.odttf"] = o_reg
    parts["word/fonts/font2.odttf"] = o_bold

    ct = parts.get("[Content_Types].xml")
    if ct:
        parts["[Content_Types].xml"] = _ensure_odttf_content_type(ct)

    tmp = path.with_suffix(path.suffix + ".tmp")
    with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED) as zout:
        for name in sorted(parts.keys()):
            zout.writestr(name, parts[name])
    tmp.replace(path)
    logger.info("Embedded Libre Baskerville in %s", path)
