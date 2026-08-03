"""
ir.attachment sanitization: create_uid gate + placeholder content.

Run via: odoo shell -d <db> --db_host ... --db_user odoo --db_password ... --no-http < rule_attachment.py

Rule (established and validated earlier — see ROADMAP.md):
  - create_uid = 1 (Odoo's SUPERUSER_ID / __system__, used by every module's
    install/data-loading code as a core convention) -> module-shipped asset
    (icon, demo photo, report template). Never touch.
  - create_uid != 1 -> created by a real logged-in user during actual
    business use. Real content — needs sanitizing.

For the real ones: replace the actual file content in the filestore with a
genuinely valid minimal placeholder appropriate to the mimetype (not just
garbage bytes — a placeholder PDF should still open as a PDF), recompute
the checksum, and update the ir_attachment row to point at it. Content-
addressed storage means every attachment of the same mimetype collapses to
ONE shared placeholder file — this is a feature, not a bug (matches Odoo's
own natural deduplication of identical content), and means we only write
each placeholder to disk once, not once per row.

Scope, based on real mimetype survey on `orion_test` (577 real attachments):
  application/pdf                          244  -> genuine minimal valid PDF
  application/json                         151  -> valid JSON
  image/png                                 63  -> genuine minimal valid PNG
  image/jpeg                                53  -> genuine minimal valid JPEG
  everything else (docx/odt/xlsx/zip/...)   66  -> generic text placeholder
                                                    (not format-valid — these
                                                    are a small tail, and
                                                    building fully valid
                                                    zip-based office-doc
                                                    placeholders was judged
                                                    not worth the complexity
                                                    for a ~11% minority)
"""
import hashlib
import struct
import zlib


def _build_minimal_png() -> bytes:
    """A genuinely valid 1x1 white PNG, built from scratch (not a hand-typed
    binary constant, which is hard to keep correct through repeated edits)."""
    def chunk(tag, data):
        c = tag + data
        return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)

    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)  # 1x1, 8-bit, RGB
    raw_scanline = b"\x00" + b"\xff\xff\xff"  # filter byte + one white RGB pixel
    idat = zlib.compress(raw_scanline)
    return sig + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")


def _build_minimal_jpeg() -> bytes:
    """A minimal structurally-valid grayscale JPEG (SOI/APP0/DQT/SOF/DHT/SOS/EOI).
    Not photographically meaningful — just needs to open as a real JPEG."""
    soi = b"\xff\xd8"
    app0 = b"\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    dqt = b"\xff\xdb\x00\x43\x00" + bytes([1] * 64)
    sof0 = b"\xff\xc0\x00\x0b\x08\x00\x01\x00\x01\x01\x01\x11\x00"
    dht = b"\xff\xc4\x00\x14\x00" + bytes([0] * 16) + b"\x00"
    sos = b"\xff\xda\x00\x08\x01\x01\x00\x00\x3f\x00"
    scan_data = b"\xd2\xcf\x20"
    eoi = b"\xff\xd9"
    return soi + app0 + dqt + sof0 + dht + sos + scan_data + eoi


def _build_minimal_pdf() -> bytes:
    body = (
        b"%PDF-1.4\n"
        b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
        b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
        b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 200]/Contents 4 0 R/Resources<<>>>>endobj\n"
        b"4 0 obj<</Length 44>>stream\n"
        b"BT /F1 12 Tf 20 100 Td (Test placeholder) Tj ET\n"
        b"endstream endobj\n"
        b"trailer<</Size 5/Root 1 0 R>>\n"
        b"%%EOF"
    )
    return body


PLACEHOLDERS_BY_MIMETYPE = {
    "application/pdf": _build_minimal_pdf(),
    "application/json": b'{"placeholder": true}',
    "image/png": _build_minimal_png(),
    "image/jpeg": _build_minimal_jpeg(),
}
GENERIC_PLACEHOLDER = b"Test placeholder content."


def _placeholder_for(mimetype: str) -> bytes:
    return PLACEHOLDERS_BY_MIMETYPE.get(mimetype, GENERIC_PLACEHOLDER)


def _write_to_filestore(env, db_name: str, content: bytes) -> tuple:
    """Write content to the filestore (content-addressed by its own checksum),
    returning (checksum, store_fname, size). No-op if already present —
    that's the natural deduplication."""
    import os

    checksum = hashlib.sha1(content).hexdigest()
    store_fname = f"{checksum[:2]}/{checksum}"
    filestore_root = env["ir.attachment"]._filestore()
    full_path = os.path.join(filestore_root, store_fname)
    os.makedirs(os.path.dirname(full_path), exist_ok=True)
    if not os.path.exists(full_path):
        with open(full_path, "wb") as f:
            f.write(content)
    return checksum, store_fname, len(content)


def apply_attachment_sanitization(env):
    # Raw SQL, not env['ir.attachment'].search() — the ORM's search() has a
    # hidden business-logic filter that excludes attachments backing a
    # Binary field's own storage (res_field set, e.g. a real contact's
    # uploaded photo in res.partner.image_1920). Found via a real discrepancy:
    # ORM search found 473, a direct SQL count found 553 — the missing 80
    # were all real, genuine content (actual uploaded photos), exactly what
    # this rule exists to sanitize. Raw SQL sees everything, no hidden filter.
    env.cr.execute("select id, mimetype from ir_attachment where create_uid != 1 and type = 'binary'")
    real_attachments = env.cr.fetchall()
    print(f"Real (create_uid != 1) binary attachments found: {len(real_attachments)}")

    # Precompute one placeholder write per distinct mimetype (dedup by content).
    cache = {}
    total_written = 0
    total_errors = 0

    for att_id, mimetype in real_attachments:
        mimetype = mimetype or ""
        try:
            if mimetype not in cache:
                content = _placeholder_for(mimetype)
                cache[mimetype] = _write_to_filestore(env, env.cr.dbname, content)
            checksum, store_fname, size = cache[mimetype]
            env.cr.execute(
                'UPDATE ir_attachment SET store_fname = %s, checksum = %s, file_size = %s WHERE id = %s',
                (store_fname, checksum, size, att_id),
            )
            env.cr.commit()  # immediately — same fix as Rule 1/2: a later
            # record's rollback must never be able to discard this write.
            total_written += 1
        except Exception as e:
            env.cr.rollback()
            total_errors += 1
            print(f"ERROR on ir.attachment({att_id}) mimetype={mimetype}: {type(e).__name__}: {e}")

    env.cr.commit()
    print(f"Attachment sanitization done: {total_written} written, {total_errors} errors")
    print(f"Distinct placeholders written: {list(cache.keys())}")

    # ir_attachment.index_content is Odoo's cached full-text-search extraction
    # of the file's content -- a SEPARATE field from the actual file bytes,
    # populated once when the file is first indexed and never touched again
    # by anything above. Replacing the file content does NOT clear this --
    # confirmed via a real, serious leak: a genuine customer PO's fully
    # readable extracted text (company name, address, email, CIN number)
    # was still sitting here in plain text after the file-content swap,
    # because substring_hunt_scan.py blanket-excludes every ir_* table.
    # Same create_uid != 1 gate as the file-content rule above -- system-
    # shipped attachments (create_uid = 1) never hold real business content
    # to begin with, so their index_content is left alone.
    env.cr.execute(
        "UPDATE ir_attachment SET index_content = NULL WHERE create_uid != 1 AND index_content IS NOT NULL"
    )
    index_content_cleared = env.cr.rowcount
    env.cr.commit()
    print(f"ir_attachment.index_content: {index_content_cleared} row(s) cleared")

    return total_written, total_errors


if __name__ == "__main__":
    apply_attachment_sanitization(env)  # noqa: F821
