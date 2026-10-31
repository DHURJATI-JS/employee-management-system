"""Profile pictures, company logos and payout proof screenshots. The upload has already been virus-scanned and saved by @as_form; here
Pillow decodes it in a separate, isolated Python process (-I) with a time limit and a pixel cap (no decompression bombs), and re-encodes
it as a fresh WebP: pictures/logos cropped to 256x256, proofs kept whole (at most 2000 px, so the text stays readable). Only real
JPEG/PNG/WebP/GIF/BMP images decode (an SVG or a renamed file is rejected), all metadata (EXIF, GPS, camera) is dropped, and only the
re-encoded file is kept: the raw upload is deleted. File names come from system.media_file / proof_file (HMACs), so nothing about them
is stored in Postgres and their URLs can't be guessed. Proofs live outside /media and are only served to the people allowed to see them."""
import asyncio, contextlib, json, os, secrets, subprocess, sys, urllib.error, urllib.request
from pathlib import Path
from system import UPLOAD_DIR, id_file, media_file, proof_file
SIZE, PROOF_SIZE, TIMEOUT_S, MAX_PIXELS = 256, 2000, 20, 40_000_000
FORMATS = ("JPEG", "PNG", "WEBP", "GIF", "BMP")
def _worker(resize: str, quality: int) -> str:
    return ("import sys, warnings\nfrom PIL import Image, ImageOps\nwarnings.simplefilter('error', Image.DecompressionBombWarning)\n"
            f"Image.MAX_IMAGE_PIXELS = {MAX_PIXELS}\nsrc, dst = sys.argv[1], sys.argv[2]\n"
            f"with Image.open(src, formats={FORMATS!r}) as im:\n    im.seek(0)\n    im = ImageOps.exif_transpose(im).convert('RGBA')\n"
            f"    {resize}.save(dst, 'WEBP', quality={quality}, method=4)\n")  # no exif= passed: metadata is gone
_PICTURE = _worker(f"ImageOps.fit(im, ({SIZE}, {SIZE}), Image.Resampling.LANCZOS)", 86)
_PROOF = _worker(f"(ImageOps.contain(im, ({PROOF_SIZE}, {PROOF_SIZE}), Image.Resampling.LANCZOS) if max(im.size) > {PROOF_SIZE} else im)", 90)
_SHOP = _worker("(ImageOps.contain(im, (1400, 1400), Image.Resampling.LANCZOS) if max(im.size) > 1400 else im)", 86)  # shop photos: whole, at most 1400 px
_THUMB = _worker("ImageOps.contain(im, (480, 480), Image.Resampling.LANCZOS)", 80)  # ... and their card-sized copy
class PictureError(Exception): pass
def _run(worker: str, src: str, dst: str) -> None:
    try: done = subprocess.run([sys.executable, "-I", "-c", worker, src, dst], capture_output=True, timeout=TIMEOUT_S)
    except subprocess.TimeoutExpired: raise PictureError("That picture took too long to process.") from None
    if done.returncode:
        err = done.stderr.decode("utf-8", "replace")
        raise PictureError("That picture is too large (too many pixels)." if "DecompressionBomb" in err else "That file isn't a picture we can use: upload a JPEG, PNG, WebP or GIF.")
async def _store(upload_name: str, dst: Path, worker: str) -> None:
    src, tmp = UPLOAD_DIR / upload_name, dst.parent / f".{secrets.token_hex(8)}.tmp.webp"  # dot-name + atomic replace: a half-written file is never served
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(_run, worker, str(src), str(tmp))
        os.replace(tmp, dst)
    finally:
        for p in (src, tmp):
            with contextlib.suppress(OSError): p.unlink(missing_ok=True)
async def store_picture(upload_name: str, kind: str, key) -> None:
    """Re-encode the saved upload into kind/key's picture (replacing any old one). The raw upload is always deleted."""
    await _store(upload_name, media_file(kind, key), _PICTURE)
async def store_proof(upload_name: str, record_id: int, kind: str = "proof") -> None:
    """Re-encode a transaction screenshot: a payout's ("proof") or a deposit's ("deposit"). PROOF_DIR, not public. The raw upload is always deleted."""
    await _store(upload_name, proof_file(record_id, kind), _PROOF)
async def store_id_photo(upload_name: str, kind: str, key) -> None:
    """Re-encode a photo of an identity proof: a person's ("u") or a company's ("c"). Not public. The raw upload is always deleted."""
    await _store(upload_name, id_file(kind, key), _PROOF)
async def store_ai_photo(upload_name: str, dst: Path) -> None:
    """A picture shown to the Deminic AI (deminic.py): re-encoded whole (at most 1400 px), private to its owner. The raw upload is always deleted."""
    await _store(upload_name, dst, _SHOP)
async def store_shop_photo(upload_name: str, full: Path, thumb: Path) -> None:
    """A product / review photo: re-encoded whole (at most 1400 px) + a 480 px thumbnail; the raw upload is always deleted."""
    src, tmps = UPLOAD_DIR / upload_name, [full.parent / f".{secrets.token_hex(8)}.tmp.webp" for _ in range(2)]
    try:
        full.parent.mkdir(parents=True, exist_ok=True)
        for worker, tmp in zip((_SHOP, _THUMB), tmps): await asyncio.to_thread(_run, worker, str(src), str(tmp))
        os.replace(tmps[0], full); os.replace(tmps[1], thumb)
    finally:
        for p in (src, *tmps):
            with contextlib.suppress(OSError): p.unlink(missing_ok=True)
MAX_VIDEO_S = 180  # product + review videos: at most 3 minutes
def _video(src: Path) -> tuple[bytes, str]:  # the media service (whisper_service/server.py /video): remuxed into a clean MP4 / WebM, metadata dropped
    from meet import WHISPER_URL
    req = urllib.request.Request(f"{WHISPER_URL}/video?max_seconds={MAX_VIDEO_S}", src.read_bytes(), {"Content-Type": "application/octet-stream"})
    try:
        with urllib.request.urlopen(req, timeout=180) as r: return r.read(), r.headers.get("X-Ext", "mp4")
    except urllib.error.HTTPError as e:
        try: why = json.loads(e.read()).get("detail", "")
        except ValueError: why = ""
        raise PictureError(why or "That file isn't a video we can use: upload an MP4 (H.264) or WebM.") from None
    except OSError: raise PictureError("Videos can't be processed right now (the media service isn't running): try again later.") from None
async def store_video(upload_name: str, target) -> Path:
    """A product / review video, checked + remuxed by the media service (it must be a real video that browsers play, at most MAX_VIDEO_S);
    target(ext) -> where it goes. The raw upload is always deleted. Returns the stored file."""
    src = UPLOAD_DIR / upload_name
    try:
        data, ext = await asyncio.to_thread(_video, src)
        if ext not in ("mp4", "webm"): raise PictureError("That video came back in an unexpected format.")
        dst = target(ext); dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.parent / f".{secrets.token_hex(8)}.tmp"
        tmp.write_bytes(data); os.replace(tmp, dst)
        return dst
    finally:
        with contextlib.suppress(OSError): src.unlink(missing_ok=True)
def remove_id_photo(kind: str, key) -> None:
    with contextlib.suppress(OSError): id_file(kind, key).unlink(missing_ok=True)
def discard_upload(upload_name: str) -> None:  # a raw upload we won't use (e.g. the uploader wasn't allowed)
    with contextlib.suppress(OSError):
        if (p := (UPLOAD_DIR / upload_name).resolve()).parent == UPLOAD_DIR.resolve(): p.unlink(missing_ok=True)
def remove_picture(kind: str, key) -> bool:
    try: media_file(kind, key).unlink(); return True
    except FileNotFoundError: return False
def remove_proof(record_id: int, kind: str = "proof") -> None:
    with contextlib.suppress(OSError): proof_file(record_id, kind).unlink(missing_ok=True)
