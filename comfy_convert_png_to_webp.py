# ComfyUI PNG → WebP converter — correct EXIF format for drag-and-drop workflow loading
#
# HOW TO USE:
#   Place this script in the directory containing your ComfyUI PNG outputs and run it.
#   Converted WebPs are saved to a "webp_out" subdirectory next to the script.
#   Already-converted files are skipped (delete the webp to re-convert).
#
# What ComfyUI's pnginfo.ts actually requires:
#
#   1. TWO EXIF fields, both ASCII (type 2), written CONSECUTIVELY with nothing between them:
#        IFD0 tag 0x010E  ImageDescription  →  "workflow:{json}"
#        IFD0 tag 0x010F  Make              →  "prompt:{json}"
#
#   2. The value format is EXACTLY  "<key>:<json>"  — no space after the colon,
#      no newlines inside the JSON.  ComfyUI splits on the FIRST colon to get key/value.
#
#   3. NOTHING else may live in IFD0 between those two tags.
#      piexif will sort IFD0 by tag number (0x010E < 0x010F) so they will be adjacent
#      as long as you don't add other IFD0 tags.
#
#   4. Any non-ASCII EXIF entry (UTF-16 XPComment, Exif IFD UserComment, etc.) causes
#      ComfyUI's parser to add `undefined` to its result object, then crash with
#      TypeError when it tries to call .indexOf(':') on it.  That is the silent failure
#      that killed every previous attempt that included extra fields.
#
#   5. ComfyUI cannot load lossless WebP (bug in pnginfo.js / app.js as of mid-2024).
#      Keep WEBP_LOSSLESS = False.
#
# REQUIREMENTS:
#   pip install Pillow piexif

import json
import logging
import os
import re
from pathlib import Path

import piexif
from PIL import Image

# ─── Configuration ──────────────────────────────────────────────────────────────
WEBP_LOSSLESS      = False   # !! Must be False — ComfyUI cannot load lossless WebP !!
WEBP_LOSSY_QUALITY = 95      # 1–100
SAVE_AS_ANIMATED   = True    # Discord EXIF-preservation hack — no visible downside
SCRIPT_DIR         = Path(__file__).resolve().parent
OUTPUT_DIR         = SCRIPT_DIR / "webp_out"

# ─── Logging ────────────────────────────────────────────────────────────────────
RESET = "\033[0m"
COLORS = {"DEBUG": "\033[92m", "INFO": "\033[94m", "WARNING": "\033[93m",
          "ERROR": "\033[91m", "CRITICAL": "\033[95m"}

class ColorFormatter(logging.Formatter):
    def format(self, record):
        return f"{COLORS.get(record.levelname, RESET)}{super().format(record)}{RESET}"

_h = logging.StreamHandler()
_h.setFormatter(ColorFormatter("%(levelname)s: %(message)s"))
logging.root.handlers = []
logging.root.addHandler(_h)
logging.root.setLevel(logging.INFO)
log = logging.getLogger(__name__)


# ─── Helpers ─────────────────────────────────────────────────────────────────────
def _compact_json(s: str) -> str:
    """Re-serialise JSON with no extra whitespace or newlines.
    ComfyUI's parser splits on the first ':' in the EXIF value string, so the
    JSON itself must not contain bare newlines (it normally doesn't, but be safe).
    """
    try:
        return json.dumps(json.loads(s), separators=(",", ":"))
    except (json.JSONDecodeError, TypeError):
        # Not valid JSON — return as-is (e.g. A1111 parameters text)
        return s.replace("\r\n", " ").replace("\n", " ").replace("\r", " ")


def extract_comfy_metadata(img: Image.Image) -> dict[str, str]:
    """Extract ComfyUI tEXt chunks from a PNG.
    img.info catches both compressed (zTXt) and uncompressed (tEXt) chunks.
    """
    found = {}
    for key in ("workflow", "prompt", "parameters"):
        val = img.info.get(key)
        if val:
            found[key] = val
    return found


def build_exif(metadata: dict[str, str]) -> bytes | None:
    """
    Build EXIF bytes that ComfyUI's pnginfo.ts will correctly parse from WebP.

    ComfyUI reads IFD0 and for every tag it finds:
        1. Skips anything that isn't ASCII (type 2) — but broken versions crash instead
        2. Calls value.indexOf(':') to split key from JSON
        3. Stores txt_chunks[key] = json_string

    So the values must be:
        ImageDescription (0x010E):  "workflow:{...}"
        Make             (0x010F):  "prompt:{...}"

    Rules:
        - ASCII only, no UTF-16 fields anywhere in IFD0
        - No other IFD0 tags (keeps the two adjacent, avoids the crash bug)
        - JSON must be compact (no bare newlines)
    """
    workflow  = metadata.get("workflow", "")
    prompt    = metadata.get("prompt", "")
    # A1111-style images have neither; skip gracefully
    if not workflow and not prompt:
        return None

    ifd0 = {}

    if workflow:
        compact = _compact_json(workflow)
        # Value format:  "workflow:{json}"  — no space
        ifd0[piexif.ImageIFD.ImageDescription] = f"workflow:{compact}".encode("ascii", errors="replace")

    if prompt:
        compact = _compact_json(prompt)
        # Value format:  "prompt:{json}"
        ifd0[piexif.ImageIFD.Make] = f"prompt:{compact}".encode("ascii", errors="replace")

    if not ifd0:
        return None

    try:
        return piexif.dump({"0th": ifd0})
    except Exception as exc:
        log.warning(f"piexif.dump failed: {exc}")
        return None


# ─── Verification ────────────────────────────────────────────────────────────────
def verify_webp(path: Path) -> bool:
    """Re-open the saved WebP and confirm ComfyUI-readable metadata is present."""
    try:
        with Image.open(path) as img:
            raw_exif = img.info.get("exif", b"")
        if not raw_exif:
            log.warning(f"  ⚠ No EXIF in output: {path.name}")
            return False
        exif = piexif.load(raw_exif)
        ifd0 = exif.get("0th", {})
        desc = ifd0.get(piexif.ImageIFD.ImageDescription, b"").decode("ascii", errors="replace")
        make = ifd0.get(piexif.ImageIFD.Make, b"").decode("ascii", errors="replace")
        ok_w = desc.startswith("workflow:")
        ok_p = make.startswith("prompt:")
        if ok_w:
            log.info(f"  ✓ workflow field present ({len(desc)} chars)")
        else:
            log.warning(f"  ⚠ ImageDescription does not start with 'workflow:' — got: {desc[:60]!r}")
        if ok_p:
            log.info(f"  ✓ prompt field present ({len(make)} chars)")
        # Having at least the workflow field is enough for ComfyUI
        return ok_w
    except Exception as exc:
        log.warning(f"  ⚠ Verification error: {exc}")
        return False


# ─── Single-file conversion ───────────────────────────────────────────────────────
def convert(png_path: Path) -> bool:
    rel      = png_path.relative_to(SCRIPT_DIR)
    out_path = OUTPUT_DIR / rel.with_suffix(".webp")

    if out_path.exists():
        log.info(f"Skip (exists): {rel}")
        return True

    try:
        with Image.open(png_path) as img:
            metadata = extract_comfy_metadata(img)
            mode     = img.mode

            if not metadata:
                log.warning(f"No ComfyUI metadata in {png_path.name} — converting image only")

            # Normalise mode
            if mode not in ("RGB", "RGBA", "L"):
                img = img.convert("RGB")
                mode = "RGB"

            exif_bytes = build_exif(metadata)
            icc        = img.info.get("icc_profile")

            out_path.parent.mkdir(parents=True, exist_ok=True)

            save_opts: dict = {
                "method":  6,
                "lossless": False,
                "quality":  WEBP_LOSSY_QUALITY,
            }
            if mode == "RGBA":
                save_opts["alpha_quality"] = 100
            if icc:
                save_opts["icc_profile"] = icc
            if exif_bytes:
                save_opts["exif"] = exif_bytes
            if SAVE_AS_ANIMATED:
                save_opts.update(save_all=True, append_images=[],
                                 duration=200, loop=0, minimize_size=False)

            img.save(out_path, "WEBP", **save_opts)

        tag = "✓" if verify_webp(out_path) else "⚠"
        log.info(f"{tag} {rel} → {out_path.name}")
        return True

    except Exception as exc:
        log.error(f"✗ {png_path.name}: {exc}")
        return False


# ─── Batch ───────────────────────────────────────────────────────────────────────
def process_directory(root: Path):
    pngs = [p for p in sorted(root.rglob("*.png"))
            if OUTPUT_DIR not in p.parents]
    if not pngs:
        log.warning(f"No PNG files found under {root}")
        return

    log.info(f"Found {len(pngs)} PNG(s)\n")
    ok = err = 0
    for p in pngs:
        if convert(p):
            ok += 1
        else:
            err += 1

    print(f"\n{'─'*55}")
    print(f"  Done.  ✓ {ok} converted   ✗ {err} errors")
    print(f"  Output → {OUTPUT_DIR}")
    print(f"{'─'*55}")


# ─── Settings summary ─────────────────────────────────────────────────────────────
def print_settings():
    CYAN, WHITE, GREEN, RED = "\033[96m", "\033[97m", "\033[92m", "\033[91m"
    rows = [
        ("Mode",         f"Lossy (quality={WEBP_LOSSY_QUALITY})"),
        ("Animated hack", str(SAVE_AS_ANIMATED)),
        ("Source dir",   str(SCRIPT_DIR)),
        ("Output dir",   str(OUTPUT_DIR)),
        ("EXIF layout",  "IFD0/ImageDescription=workflow + IFD0/Make=prompt (ASCII only)"),
    ]
    print(f"\n{CYAN}{'─'*55}{RESET}")
    for k, v in rows:
        vc = (GREEN if v == "True" else RED) if v in ("True", "False") else WHITE
        print(f"  {CYAN}{k:<16}{RESET} {vc}{v}{RESET}")
    print(f"{CYAN}{'─'*55}{RESET}\n")


if __name__ == "__main__":
    print_settings()
    process_directory(SCRIPT_DIR)
    input("\nPress Enter to exit...")