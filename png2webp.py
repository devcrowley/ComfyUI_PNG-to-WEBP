# ComfyUI PNG → WebP converter — correct EXIF format for drag-and-drop workflow loading
#
# HOW TO USE:
#   Place this script in the directory containing your ComfyUI PNG outputs and run it.
#   On startup it will ask whether to delete the original PNGs after conversion.
#     - Yes: WebP is saved alongside the PNG, then PNG is deleted after verified conversion
#     - No:  WebP is saved to a "webp_out" subdirectory (original PNGs untouched)
#   Already-converted files are skipped (delete the webp to re-convert).
#   Conversion runs in parallel across all available logical CPU cores.
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
#      TypeError when it tries to call .indexOf(':') on it.
#
#   5. ComfyUI cannot load lossless WebP (bug in pnginfo.js / app.js as of mid-2024).
#      Keep WEBP_LOSSLESS = False.
#
# REQUIREMENTS:
#   pip install Pillow piexif

import json
import logging
import os
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import piexif
from PIL import Image

# ─── Configuration ──────────────────────────────────────────────────────────────
WEBP_LOSSY_QUALITY = 80    # 1–100
SAVE_AS_ANIMATED   = True  # Discord EXIF-preservation hack — no visible downside
SCRIPT_DIR         = Path(__file__).resolve().parent
OUTPUT_SUBDIR      = "webp_out"  # used only when NOT replacing originals

# ─── Logging ────────────────────────────────────────────────────────────────────
RESET  = "\033[0m"
COLORS = {"DEBUG": "\033[92m", "INFO": "\033[94m", "WARNING": "\033[93m",
          "ERROR": "\033[91m", "CRITICAL": "\033[95m"}

class ColorFormatter(logging.Formatter):
    def format(self, record):
        return f"{COLORS.get(record.levelname, RESET)}{super().format(record)}{RESET}"

def _setup_logging():
    h = logging.StreamHandler()
    h.setFormatter(ColorFormatter("%(levelname)s: %(message)s"))
    logging.root.handlers = []
    logging.root.addHandler(h)
    logging.root.setLevel(logging.INFO)


# ─── Pure functions (safe to call in worker processes) ───────────────────────────
def _compact_json(s: str) -> str:
    try:
        return json.dumps(json.loads(s), separators=(",", ":"))
    except (json.JSONDecodeError, TypeError):
        return s.replace("\r\n", " ").replace("\n", " ").replace("\r", " ")


def extract_comfy_metadata(img: Image.Image) -> dict:
    found = {}
    for key in ("workflow", "prompt", "parameters"):
        val = img.info.get(key)
        if val:
            found[key] = val
    return found


def build_exif(metadata: dict) -> bytes | None:
    workflow = metadata.get("workflow", "")
    prompt   = metadata.get("prompt", "")
    if not workflow and not prompt:
        return None
    ifd0 = {}
    if workflow:
        ifd0[piexif.ImageIFD.ImageDescription] = \
            f"workflow:{_compact_json(workflow)}".encode("ascii", errors="replace")
    if prompt:
        ifd0[piexif.ImageIFD.Make] = \
            f"prompt:{_compact_json(prompt)}".encode("ascii", errors="replace")
    try:
        return piexif.dump({"0th": ifd0})
    except Exception:
        return None


def verify_webp(path: Path) -> bool:
    try:
        with Image.open(path) as img:
            raw_exif = img.info.get("exif", b"")
        if not raw_exif:
            return False
        exif = piexif.load(raw_exif)
        ifd0 = exif.get("0th", {})
        desc = ifd0.get(piexif.ImageIFD.ImageDescription, b"").decode("ascii", errors="replace")
        return desc.startswith("workflow:")
    except Exception:
        return False


# ─── Worker ──────────────────────────────────────────────────────────────────────
# NOTE: On Windows, ProcessPoolExecutor uses 'spawn' to create worker processes,
# which means each worker imports this entire module from scratch. The worker
# function must be a plain top-level function (not nested/lambda) so pickle can
# find it by name. The if __name__ == "__main__" guard at the bottom is what
# prevents workers from re-running the interactive prompts and hanging silently.

def _worker(args: tuple) -> tuple:
    """
    Convert one PNG to WebP.
    Returns (png_path_str, webp_path_str, status, had_metadata)
      status: "ok" | "skipped" | "failed" | "failed:<reason>"
    """
    png_str, webp_str, delete_originals = args
    png_path  = Path(png_str)
    webp_path = Path(webp_str)

    if webp_path.exists():
        return png_str, webp_str, "skipped", False

    try:
        with Image.open(png_path) as img:
            metadata = extract_comfy_metadata(img)
            mode     = img.mode
            if mode not in ("RGB", "RGBA", "L"):
                img = img.convert("RGB")
                mode = "RGB"

            exif_bytes = build_exif(metadata)
            icc        = img.info.get("icc_profile")

            webp_path.parent.mkdir(parents=True, exist_ok=True)

            save_opts: dict = {
                "method":   6,
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

            img.save(webp_path, "WEBP", **save_opts)

        ok = verify_webp(webp_path)
        if not ok:
            webp_path.unlink(missing_ok=True)
            return png_str, webp_str, "failed:verification failed", bool(metadata)

        if delete_originals:
            png_path.unlink()

        return png_str, webp_str, "ok", bool(metadata)

    except Exception as exc:
        try:
            webp_path.unlink(missing_ok=True)
        except Exception:
            pass
        return png_str, webp_str, f"failed:{exc}", False


# ─── UI helpers ──────────────────────────────────────────────────────────────────
def ask_delete_originals() -> bool:
    CYAN   = "\033[96m"
    YELLOW = "\033[93m"
    GREEN  = "\033[92m"
    RED    = "\033[91m"
    print(f"\n{CYAN}{'─'*55}{RESET}")
    print(f"  {YELLOW}Delete original PNGs after verified conversion?{RESET}")
    print(f"  {GREEN}y{RESET} — WebP saved in-place, PNG deleted once conversion is confirmed")
    print(f"  {RED}n{RESET} — WebP saved to '{OUTPUT_SUBDIR}/' subfolder, PNGs untouched")
    print(f"{CYAN}{'─'*55}{RESET}")
    while True:
        choice = input("  Choice [y/n]: ").strip().lower()
        if choice in ("y", "yes"):
            print(f"\n  {YELLOW}⚠  Original PNGs will be permanently deleted after conversion.{RESET}")
            confirm = input("  Type YES to confirm: ").strip()
            if confirm == "YES":
                return True
            print("  Cancelled — defaulting to keep originals.\n")
            return False
        elif choice in ("n", "no", ""):
            return False
        print("  Please enter y or n.")


def print_settings(delete_originals: bool, workers: int):
    CYAN  = "\033[96m"
    WHITE = "\033[97m"
    GREEN = "\033[92m"
    RED   = "\033[91m"
    dest  = "In-place (PNG deleted after verify)" if delete_originals else f"{OUTPUT_SUBDIR}/"
    rows  = [
        ("Quality",       f"Lossy {WEBP_LOSSY_QUALITY}"),
        ("Animated hack", str(SAVE_AS_ANIMATED)),
        ("Destination",   dest),
        ("Workers",       f"{workers} logical cores"),
        ("Source dir",    str(SCRIPT_DIR)),
    ]
    print(f"\n{CYAN}{'─'*55}{RESET}")
    for k, v in rows:
        vc = (GREEN if v == "True" else RED) if v in ("True", "False") else WHITE
        print(f"  {CYAN}{k:<16}{RESET} {vc}{v}{RESET}")
    print(f"{CYAN}{'─'*55}{RESET}\n")


# ─── Entry point ─────────────────────────────────────────────────────────────────
# CRITICAL: This guard must wrap ALL interactive code.
# On Windows, every worker process spawned by ProcessPoolExecutor re-imports this
# module. Without this guard, each worker hits ask_delete_originals(), blocks on
# input(), and the whole thing silently hangs with no output and no errors —
# exactly the symptom reported. The guard ensures only the original process runs
# the interactive UI; worker processes import the module and stop here.

if __name__ == "__main__":
    _setup_logging()
    log = logging.getLogger(__name__)

    workers          = os.cpu_count() or 1
    delete_originals = ask_delete_originals()
    print_settings(delete_originals, workers)

    root       = SCRIPT_DIR
    output_dir = root if delete_originals else root / OUTPUT_SUBDIR

    pngs = [p for p in sorted(root.rglob("*.png"))
            if (root / OUTPUT_SUBDIR) not in p.parents]

    if not pngs:
        log.warning(f"No PNG files found under {root}")
        input("\nPress Enter to exit...")
        raise SystemExit

    tasks   = []
    skipped = 0
    for png in pngs:
        rel  = png.relative_to(root)
        webp = (output_dir / rel).with_suffix(".webp")
        if webp.exists():
            skipped += 1
        else:
            tasks.append((str(png), str(webp), delete_originals))

    log.info(f"Found {len(pngs)} PNG(s) — {skipped} already converted, {len(tasks)} to process\n")

    if not tasks:
        print("Nothing to do.")
        input("\nPress Enter to exit...")
        raise SystemExit

    ok = err = 0

    with ProcessPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_worker, t): t for t in tasks}
        done = 0
        for future in as_completed(futures):
            done += 1
            png_str, webp_str, status, had_meta = future.result()
            png_name = Path(png_str).name
            meta_tag = "" if had_meta else " (no metadata)"
            if status == "ok":
                ok += 1
                action = "deleted PNG" if delete_originals else f"→ {OUTPUT_SUBDIR}/"
                log.info(f"[{done}/{len(tasks)}] ✓ {png_name} {action}{meta_tag}")
            elif status == "skipped":
                skipped += 1
                log.info(f"[{done}/{len(tasks)}] ⏭ {png_name} already exists")
            else:
                err += 1
                detail = status.replace("failed:", "").strip()
                log.error(f"[{done}/{len(tasks)}] ✗ {png_name}{f' — {detail}' if detail else ''}")

    print(f"\n{'─'*55}")
    print(f"  Done.  ✓ {ok} converted   ✗ {err} errors   ⏭ {skipped} skipped")
    if not delete_originals:
        print(f"  Output → {output_dir}")
    print(f"{'─'*55}")
    input("\nPress Enter to exit...")
