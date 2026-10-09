"""
MMF2 / Clickteam Fusion 2 ImageBank reader and writer.

Layout (verified against CTFAK's own decoder, see docs/FORMAT.md):

    int32   count
    repeat count:
        int32   handle            # on disk this is (logical handle + 1) when build >= 284
        int32   decompressedSize  # length of the zlib payload below
        int32   compressedSize
        byte[]  zlib(payload)

    payload:
        0   int32  checksum       # not validated by the runtime in our tests
        4   int32  references
        8   int32  dataSize       # decides the pixel layout
       12   int16  width
       14   int16  height
       16   uint8  graphicMode
       17   uint8  flags          # 1 RLE, 2 RLEW, 4 RLET, 8 LZX, 0x10 Alpha, 0x40 Mac, 0x80 RGBA
       18   int16  pad
       20   int16  hotspotX / hotspotY / actionX / actionY
       28   4B     transparent colour
       32   byte[] pixels

Pixel layouts for graphicMode == 4:
    dataSize == w*h*3              -> tight 24bpp BGR, transparency via the transparent colour
    dataSize == w*h*3 + a4(w)*h    -> TIGHT BGR plane + alpha plane padded to 4-byte rows
    dataSize == a4(w*3)*h + a4(w)*h-> both planes padded to 4-byte rows
    dataSize == w*h*4              -> interleaved BGRA
"""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass, field

import numpy as np

HEADER_SIZE = 32


def align4(x: int) -> int:
    return (x + 3) & ~3


@dataclass
class ImageRecord:
    """One raw record from the ImageBank, keeping the original bytes intact."""
    handle: int
    payload_size: int          # the record's DecompressedSize field == 32 + pixel bytes
    comp_size: int
    compressed: bytes
    payload: bytes = field(repr=False, default=b"")

    # ---- payload accessors -------------------------------------------------
    @property
    def checksum(self) -> int:      return struct.unpack_from("<i", self.payload, 0)[0]
    @property
    def references(self) -> int:    return struct.unpack_from("<i", self.payload, 4)[0]
    @property
    def data_size(self) -> int:
        """Pixel byte count - EXCLUDES the 32-byte header (unlike payload_size)."""
        return struct.unpack_from("<i", self.payload, 8)[0]
    @property
    def width(self) -> int:         return struct.unpack_from("<h", self.payload, 12)[0]
    @property
    def height(self) -> int:        return struct.unpack_from("<h", self.payload, 14)[0]
    @property
    def graphic_mode(self) -> int:  return self.payload[16]
    @property
    def flags(self) -> int:         return self.payload[17]
    @property
    def transparent(self) -> tuple: return tuple(self.payload[28:32])
    @property
    def tight_handle(self) -> int:
        """The handle as CTFAK exposes it (disk value minus one for build >= 284)."""
        return self.handle - 1

    # ---- pixel codec -------------------------------------------------------
    def to_rgba(self) -> np.ndarray:
        """Decode to an (h, w, 4) uint8 RGBA array."""
        w, h, ds = self.width, self.height, self.data_size
        pix = self.payload[HEADER_SIZE:HEADER_SIZE + ds]
        if self.graphic_mode != 4 or w == 0 or h == 0:
            raise ValueError(f"unsupported graphicMode={self.graphic_mode} for {w}x{h}")

        if ds == w * h * 3:
            bgr = np.frombuffer(pix, np.uint8).reshape(h, w, 3)
            alpha = np.full((h, w), 255, np.uint8)
        elif ds == w * h * 3 + align4(w) * h:
            bgr = np.frombuffer(pix[:w * h * 3], np.uint8).reshape(h, w, 3)
            plane = np.frombuffer(pix[w * h * 3:], np.uint8).reshape(h, align4(w))
            alpha = plane[:, :w].copy()
        elif ds == align4(w * 3) * h + align4(w) * h:
            bgr = np.frombuffer(pix[:align4(w * 3) * h], np.uint8) \
                    .reshape(h, align4(w * 3))[:, :w * 3].reshape(h, w, 3).copy()
            plane = np.frombuffer(pix[align4(w * 3) * h:], np.uint8).reshape(h, align4(w))
            alpha = plane[:, :w].copy()
        elif ds == w * h * 4:
            return np.frombuffer(pix, np.uint8).reshape(h, w, 4)[:, :, [2, 1, 0, 3]].copy()
        else:
            raise ValueError(f"unknown layout dataSize={ds} for {w}x{h}")

        rgba = np.dstack([bgr[:, :, 2], bgr[:, :, 1], bgr[:, :, 0], alpha])
        # images without an alpha channel express transparency with a colour key
        if ds == w * h * 3 and self.transparent[3] == 255:
            key = np.array(self.transparent[:3], np.uint8)
            rgba[np.all(rgba[:, :, :3] == key, axis=2), 3] = 0
        return rgba

    def encode(self, rgba: np.ndarray) -> bytes:
        """Serialise an (h, w, 4) array back into a payload of the SAME length."""
        w, h, ds = self.width, self.height, self.data_size
        if rgba.shape[0] != h or rgba.shape[1] != w:
            raise ValueError(f"size mismatch: got {rgba.shape[1]}x{rgba.shape[0]}, want {w}x{h}")
        body = _encode_body_raw(rgba, ds, w, h, self.transparent,
                                transparent_is_key=self.transparent[3] == 255)
        if body is None:
            raise ValueError(f"unknown layout dataSize={ds} for {w}x{h}")
        if len(body) != ds:
            raise ValueError(f"encoded {len(body)} bytes, expected {ds}")
        return self.payload[:HEADER_SIZE] + body


def parse(bank: bytes) -> list[ImageRecord]:
    """Parse an ImageBank chunk payload into records."""
    pos = 0
    count = struct.unpack_from("<i", bank, pos)[0]
    pos += 4
    records: list[ImageRecord] = []
    for _ in range(count):
        handle, dsize, csize = struct.unpack_from("<iiI", bank, pos)
        pos += 12
        comp = bank[pos:pos + csize]
        pos += csize
        rec = ImageRecord(handle=handle, payload_size=dsize, comp_size=csize, compressed=comp)
        rec.payload = zlib.decompress(comp)
        records.append(rec)
    if pos != len(bank):
        raise ValueError(f"trailing data: consumed {pos} of {len(bank)}")
    return records


def serialise(records: list[ImageRecord]) -> bytes:
    """Rebuild the ImageBank. Total size is checked by the caller."""
    parts = [struct.pack("<i", len(records))]
    for r in records:
        parts.append(struct.pack("<iiI", r.handle, len(r.payload), len(r.compressed)))
        parts.append(r.compressed)
    return b"".join(parts)


def compress_fitting(payload: bytes, limit: int, rgba: np.ndarray | None = None,
                     transparent: tuple | None = None, w: int = 0, h: int = 0,
                     data_size: int = 0, flags: int = 0,
                     quantise_ladder=(128, 96, 64, 48, 32, 24, 16, 12, 8)) -> bytes | None:
    """
    Compress `payload` to at most `limit` bytes.

    zlib stops at ZLIB_STREAM_END, so trailing zero padding is ignored by the decoder -
    that lets a smaller result be padded back to the exact original size.

    If it does not fit, progressively quantise the image's palette (requires `rgba`);
    this keeps the total bank size - and therefore every image offset - unchanged.
    Returns None if even the coarsest palette is too big.
    """
    from PIL import Image

    def try_levels(data: bytes) -> bytes | None:
        for level, strategy in ((9, zlib.Z_DEFAULT_STRATEGY), (9, zlib.Z_FILTERED),
                                (9, zlib.Z_HUFFMAN_ONLY), (9, zlib.Z_RLE),
                                (6, zlib.Z_DEFAULT_STRATEGY)):
            c = zlib.compressobj(level, zlib.DEFLATED, 15, 9, strategy)
            out = c.compress(data) + c.flush()
            if len(out) <= limit:
                return out + b"\x00" * (limit - len(out))
        return None

    got = try_levels(payload)
    if got is not None:
        return got
    if rgba is None:
        return None

    alpha = rgba[:, :, 3]
    rgb = Image.fromarray(rgba, "RGBA").convert("RGB")
    for ncol in quantise_ladder:
        q = rgb.quantize(colors=ncol, method=Image.MEDIANCUT).convert("RGB")
        coarse = np.dstack([np.array(q), alpha])
        body = _encode_body_raw(coarse, data_size, w, h, transparent,
                                transparent_is_key=bool(transparent and transparent[3] == 255))
        if body is None:
            return None
        got = try_levels(payload[:HEADER_SIZE] + body)
        if got is not None:
            return got
    return None


def _encode_body_raw(rgba: np.ndarray, data_size: int, w: int, h: int,
                     transparent: tuple | None,
                     transparent_is_key: bool = False) -> bytes | None:
    """
    Serialise pixels into `data_size` bytes.

    Images carrying an alpha channel store TWO SEPARATE PLANES - every BGR row first
    (stride w*3, tight), then every alpha row (stride align4(w), padded). Interleaving
    them per row compiles to the same byte count but silently zeroes the alpha channel,
    which makes every affected image render fully transparent.
    """
    bgr = np.ascontiguousarray(rgba[:, :, [2, 1, 0]])
    alp = np.ascontiguousarray(rgba[:, :, 3])

    if data_size == w * h * 3 + align4(w) * h:
        flat = np.zeros(w * h * 3 + align4(w) * h, np.uint8)
        flat[:w * h * 3] = bgr.reshape(-1)
        pad = np.zeros((h, align4(w)), np.uint8)
        pad[:, :w] = alp
        flat[w * h * 3:] = pad.reshape(-1)
        return flat.tobytes()
    if data_size == align4(w * 3) * h + align4(w) * h:
        flat = np.zeros(align4(w * 3) * h + align4(w) * h, np.uint8)
        pad3 = np.zeros((h, align4(w * 3)), np.uint8)
        pad3[:, :w * 3] = bgr.reshape(h, w * 3)
        flat[:align4(w * 3) * h] = pad3.reshape(-1)
        pad1 = np.zeros((h, align4(w)), np.uint8)
        pad1[:, :w] = alp
        flat[align4(w * 3) * h:] = pad1.reshape(-1)
        return flat.tobytes()
    if data_size == w * h * 3:
        b = bgr.copy()
        if transparent and transparent_is_key:
            b[alp == 0] = np.array(transparent[:3], np.uint8)
        return b.tobytes()
    if data_size == w * h * 4:
        return np.ascontiguousarray(rgba[:, :, [2, 1, 0, 3]]).tobytes()
    return None
