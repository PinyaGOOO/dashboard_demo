"""Validated, locally stored raster illustrations for teacher-authored Markdown."""
from __future__ import annotations

import re
import secrets
import struct
import threading
import zlib

from .assignment_audience import allows_student_group, parse_stored_groups

MAX_IMAGE_BYTES = 2 * 1024 * 1024
MAX_PIXELS = 8 * 1024 * 1024
MAX_EDGE = 8192
MAX_REFS = 12
STORAGE_BYTES = 256 * 1024 * 1024
STORAGE_COUNT = 4096
STAGING_SECONDS = 24 * 3600
_VALIDATORS = threading.BoundedSemaphore(2)
_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_ID = re.compile(r"[0-9a-f]{32}")
_LAB = re.compile(r"lab-([1-9][0-9]{0,9})-([1-9][0-9]{0,8})")
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})([^\n]*)$")
_INLINE = re.compile(r"\\[\\`*_\[\]!|]|`[^`\n]{1,2048}`|(!?)\[([^\]\n]{0,512})\]\(([^()\s]{1,2048})\)")


class ImageInputError(ValueError):
    pass


class ImageLimitError(ValueError):
    pass


def valid_id(value):
    return isinstance(value, str) and _ID.fullmatch(value) is not None


def _reject():
    raise ImageInputError("Некорректный PNG. Выберите изображение ещё раз.")


def _chunk(kind, body):
    return struct.pack(">I",len(body)) + kind + body + struct.pack(">I",zlib.crc32(body,zlib.crc32(kind)))


def canonical_png(raw):
    """Accept the non-interlaced 8-bit raster emitted by browser canvas.

    Do not pass original containers (including optional metadata, trailing
    payloads or extra zlib streams) on to browsers. Validate the DEFLATE data
    against exact dimensions before rebuilding a minimal container.
    """
    if not isinstance(raw, bytes) or len(raw)>MAX_IMAGE_BYTES:
        raise ImageInputError("Максимальный размер изображения — 2 МиБ.")
    if not _VALIDATORS.acquire(timeout=2):
        raise ImageLimitError("Обрабатываются другие изображения. Попробуйте ещё раз.")
    try:
        return _canonical_png(raw)
    finally:
        _VALIDATORS.release()


def _canonical_png(raw):
    if not raw.startswith(_SIGNATURE):
        _reject()
    pos, seen, compressed = len(_SIGNATURE), 0, bytearray()
    header = None
    past_data = False
    finished = False
    while pos < len(raw):
        if pos+12 > len(raw) or seen>=4096:
            _reject()
        size, kind = struct.unpack_from(">I4s",raw,pos)
        end = pos + 8 + size
        if end+4>len(raw) or not re.fullmatch(rb"[A-Za-z]{2}[A-Z][A-Za-z]",kind):
            _reject()
        data = memoryview(raw)[pos+8:end]
        if zlib.crc32(data,zlib.crc32(kind)) != struct.unpack_from(">I",raw,end)[0]:
            _reject()
        pos, seen = end+4, seen+1
        if kind==b"IHDR":
            if seen!=1 or size!=13:
                _reject()
            width,height,depth,color,comp,filt,interlace = struct.unpack(">IIBBBBB",data)
            channels = {0:1,2:3,4:2,6:4}.get(color)
            if (not 0<width<=MAX_EDGE or not 0<height<=MAX_EDGE or width*height>MAX_PIXELS):
                raise ImageInputError("Размер изображения превышает 8 Мп или 8192 пикселя по одной стороне.")
            if depth!=8 or channels is None or (comp,filt,interlace)!=(0,0,0):
                _reject()
            header = bytes(data)
            row_bytes = 1+width*channels
        elif header is None:
            _reject()
        elif kind==b"IDAT" and not past_data:
            compressed.extend(data)
        elif kind==b"IEND":
            if size or not compressed or pos!=len(raw):
                _reject()
            finished = True
            break
        elif kind[0]&32 and kind not in (b"acTL",b"fcTL",b"fdAT",b"tRNS"):
            # Includes EXIF, textual chunks and color profiles; none are kept
            # or decompressed. Canvas emits true alpha, never a palette key.
            past_data = past_data or bool(compressed)
        else:
            _reject()
    if not finished:
        _reject()
    expected = row_bytes * height
    try:
        decoder = zlib.decompressobj()
        pixels = decoder.decompress(compressed, expected+1)
        if (len(pixels)!=expected or not decoder.eof or decoder.unused_data or decoder.unconsumed_tail
                or any(f>4 for f in pixels[::row_bytes])):
            _reject()
    except zlib.error:
        _reject()
    # The IDAT bytes have just been checked to contain one and only one image
    # stream. Retain that stream's compression rather than doubling CPU cost.
    return _SIGNATURE + _chunk(b"IHDR",header) + _chunk(b"IDAT",bytes(compressed)) + _chunk(b"IEND",b"")


def references(markdown):
    refs, fence = set(), None
    for line in markdown.replace("\r\n","\n").replace("\r","\n").split("\n"):
        m = _FENCE.match(line)
        if fence:
            if m and m[1][0]==fence[0] and len(m[1])>=len(fence) and not m[2].strip():
                fence = None
            continue
        if m:
            fence = m[1]
            continue
        for match in _INLINE.finditer(line):
            if match[1]=="!" and match[3].startswith("attachment:"):
                ident = match[3][11:]
                if valid_id(ident):
                    refs.add(ident)
    if len(refs)>MAX_REFS:
        raise ImageInputError(f"В одном задании может быть не более {MAX_REFS} разных изображений.")
    return refs


def purge(conn):
    conn.execute("""DELETE FROM assignment_images WHERE unused_since IS NOT NULL
                    AND unused_since < CAST(strftime('%s','now') AS INTEGER)-?""", (STAGING_SECONDS,))


def stage(conn, png):
    purge(conn)
    count,total = conn.execute("SELECT count(*),coalesce(sum(length(png)),0) FROM assignment_images").fetchone()
    if count>=STORAGE_COUNT or total+len(png)>STORAGE_BYTES:
        raise ImageLimitError("Хранилище изображений заданий заполнено.")
    ident = secrets.token_hex(16)
    conn.execute("INSERT INTO assignment_images(id,png,unused_since) VALUES (?,?,strftime('%s','now'))",(ident,png))
    return {"id":ident}


def link(conn, blueprint_id, markdown):
    """Replace the attachment set inside the transaction that saves the Markdown."""
    purge(conn)
    refs = references(markdown)
    if refs:
        placeholders = ",".join("?" for _ in refs)
        found = conn.execute(f"SELECT COUNT(*) FROM assignment_images WHERE id IN ({placeholders})",tuple(refs)).fetchone()[0]
        if found!=len(refs):
            raise ImageInputError("Изображение задания отсутствует или временная загрузка истекла. Загрузите его заново.")
    current = {r[0] for r in conn.execute("SELECT image_id FROM assignment_image_refs WHERE blueprint_id=?", (blueprint_id,))}
    conn.executemany("DELETE FROM assignment_image_refs WHERE blueprint_id=? AND image_id=?",((blueprint_id,i) for i in current-refs))
    conn.executemany("INSERT INTO assignment_image_refs(blueprint_id,image_id) VALUES (?,?)",((blueprint_id,i) for i in refs-current))


def published(store, assignment_id, image_id, student_group=None):
    lab = _LAB.fullmatch(assignment_id) if isinstance(assignment_id,str) else None
    if not lab or not valid_id(image_id):
        return None
    # Resolve the exact current assignment and its link in ONE SQLite read;
    # the owner of the student session is verified by the HTTP adapter.
    row = store.query_one("""SELECT img.png,b.student_groups AS audience_json FROM assignment_images img
        JOIN assignment_image_refs ref ON ref.image_id=img.id
        JOIN blueprints b ON b.id=ref.blueprint_id
        WHERE img.id=? AND b.id=? AND b.template_vmid=? AND b.student_visible=1
          AND b.status='active' AND b.assignment_template_name!='' AND b.assignment_markdown!=''""",
                          (image_id,int(lab[1]),int(lab[2])))
    return row["png"] if row and allows_student_group(parse_stored_groups(row["audience_json"]), student_group) else None
