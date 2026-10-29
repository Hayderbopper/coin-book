"""Add a scanned coin photo to the coin book.

Examples (run from anywhere, PATH_TO_SITE is the coin-book folder):

    python add_coin.py --site PATH_TO_SITE list us/cent/wheat
    python add_coin.py --site PATH_TO_SITE add us/cent/wheat/1943-S coin_cropped.png --side obverse --grade VF
    python add_coin.py --site PATH_TO_SITE remove us/cent/wheat/1943-S

For each image this writes, under images/<country>/<denomination>/<series>/<slot>/:
    <side>.<ext>          the full-resolution image, copied as-is (PNG keeps its transparency)
    <side>-thumb.webp     a small picture for the album page
    <side>.dzi + <side>_files/   zoom tiles, made for anything bigger than 3000 px

and records it in data/collection.json. FORMAT.md describes both JSON files.

Needs: pip install pillow
"""
import argparse
import datetime
import json
import math
import os
import re
import shutil
import sys

from PIL import Image

Image.MAX_IMAGE_PIXELS = None  # stitched scans are legitimately huge

SIDES = ("obverse", "reverse", "edge")
THUMB_PX = 480
TILE_PX = 254
TILE_OVERLAP = 1
TILES_ABOVE_PX = 3000


def slugify(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def expand_slots(series):
    """Turn a series' slot list into [{id, label, sub}], the same way app.js does."""
    out = []
    for s in series.get("slots", []):
        if isinstance(s, str):
            out.append({"id": slugify(s), "label": s, "sub": ""})
        elif "range" in s:
            first, last = s["range"]
            for year in range(first, last + 1):
                for mint in s.get("mints", [""]):
                    label = f"{year}-{mint}" if mint else str(year)
                    out.append({"id": slugify(label), "label": label, "sub": ""})
        else:
            out.append({"id": s.get("id") or slugify(s["label"]), "label": s["label"], "sub": s.get("sub", "")})
    return out


def load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, path)


def find_series(catalog, country_id, denom_id, series_id):
    for c in catalog["countries"]:
        if c["id"] != country_id:
            continue
        for d in c["denominations"]:
            if d["id"] != denom_id:
                continue
            for s in d["series"]:
                if s["id"] == series_id:
                    return s
    return None


def resolve_slot(catalog, path):
    """'us/cent/wheat/1909-S VDB' -> ('us/cent/wheat/1909-s-vdb', slot). Exits with a hint if unknown."""
    parts = [p for p in path.strip("/").split("/")]
    if len(parts) != 4:
        sys.exit(f"Slot must look like country/denomination/series/slot, e.g. us/cent/wheat/1943-S (got {path!r})")
    country, denom, series_id, slot_text = parts
    series = find_series(catalog, slugify(country), slugify(denom), slugify(series_id))
    if series is None:
        sys.exit(f"No series {country}/{denom}/{series_id} in catalog.json. Try: add_coin.py list")
    slots = expand_slots(series)
    want = slugify(slot_text)
    for s in slots:
        if s["id"] == want:
            return f"{slugify(country)}/{slugify(denom)}/{series['id']}/{s['id']}", s
    close = [s["id"] for s in slots if s["id"].startswith(want[:4])][:12]
    sys.exit(f"No slot {slot_text!r} in {series['name']}." + (f" Close matches: {', '.join(close)}" if close else ""))


def make_tiles(img, dzi_path, files_dir):
    """Write a Deep Zoom pyramid (what the site's zoom viewer streams for big images)."""
    if os.path.isdir(files_dir):
        shutil.rmtree(files_dir)
    w, h = img.size
    max_level = math.ceil(math.log2(max(w, h)))
    level_img = img
    for level in range(max_level, -1, -1):
        lw, lh = level_img.size
        level_dir = os.path.join(files_dir, str(level))
        os.makedirs(level_dir)
        for col in range(math.ceil(lw / TILE_PX)):
            for row in range(math.ceil(lh / TILE_PX)):
                x0 = max(col * TILE_PX - TILE_OVERLAP, 0)
                y0 = max(row * TILE_PX - TILE_OVERLAP, 0)
                x1 = min((col + 1) * TILE_PX + TILE_OVERLAP, lw)
                y1 = min((row + 1) * TILE_PX + TILE_OVERLAP, lh)
                level_img.crop((x0, y0, x1, y1)).save(
                    os.path.join(level_dir, f"{col}_{row}.webp"), quality=88, method=4)
        if level:
            level_img = level_img.resize((max(1, math.ceil(lw / 2)), max(1, math.ceil(lh / 2))), Image.LANCZOS)
    with open(dzi_path, "w", encoding="utf-8") as f:
        f.write('<?xml version="1.0" encoding="UTF-8"?>\n'
                f'<Image xmlns="http://schemas.microsoft.com/deepzoom/2008" Format="webp" '
                f'Overlap="{TILE_OVERLAP}" TileSize="{TILE_PX}"><Size Width="{w}" Height="{h}"/></Image>\n')


def cmd_add(args, site, catalog, collection):
    key, slot = resolve_slot(catalog, args.slot)
    if not os.path.isfile(args.image):
        sys.exit(f"Can't find {args.image}")
    img = Image.open(args.image)
    img.load()
    if img.mode not in ("RGB", "RGBA"):
        img = img.convert("RGBA" if "A" in img.getbands() or img.mode == "P" else "RGB")
    w, h = img.size

    rel_dir = f"images/{key}"
    out_dir = os.path.join(site, *rel_dir.split("/"))
    os.makedirs(out_dir, exist_ok=True)
    ext = os.path.splitext(args.image)[1].lower()
    if ext in (".tif", ".tiff", ".bmp"):  # browsers can't show these, so store a PNG
        ext = ".png"
        img.save(os.path.join(out_dir, args.side + ext))
    else:
        shutil.copyfile(args.image, os.path.join(out_dir, args.side + ext))
    for old in os.listdir(out_dir):  # drop a previous upload of this side in another format
        if old.startswith(args.side + ".") and old != args.side + ext and not old.endswith(".dzi"):
            os.remove(os.path.join(out_dir, old))

    thumb = img.copy()
    thumb.thumbnail((THUMB_PX, THUMB_PX), Image.LANCZOS)
    thumb.save(os.path.join(out_dir, f"{args.side}-thumb.webp"), quality=85)

    side = {
        "image": f"{rel_dir}/{args.side}{ext}",
        "thumb": f"{rel_dir}/{args.side}-thumb.webp",
        "width": w,
        "height": h,
    }
    dzi = os.path.join(out_dir, f"{args.side}.dzi")
    files_dir = os.path.join(out_dir, f"{args.side}_files")
    if not args.no_tiles and (args.tiles or max(w, h) > TILES_ABOVE_PX):
        print(f"Making zoom tiles for {w}x{h} image...")
        make_tiles(img, dzi, files_dir)
        side["tiles"] = f"{rel_dir}/{args.side}.dzi"
    else:
        if os.path.exists(dzi):
            os.remove(dzi)
        shutil.rmtree(files_dir, ignore_errors=True)

    entry = collection["coins"].setdefault(key, {"sides": {}})
    entry["sides"][args.side] = side
    entry["added"] = entry.get("added") or datetime.date.today().isoformat()
    entry["updated"] = datetime.date.today().isoformat()
    for field in ("grade", "notes"):
        value = getattr(args, field)
        if value is not None:
            entry[field] = value
    if args.microscope is not None:
        entry["scan"] = {**entry.get("scan", {}), "microscope": args.microscope}
    if args.tile_count is not None:
        entry["scan"] = {**entry.get("scan", {}), "tiles": args.tile_count}
    entry.pop("sample", None)
    save_json(os.path.join(site, "data", "collection.json"), collection)
    print(f"Added {slot['label']}{' ' + slot['sub'] if slot['sub'] else ''} ({args.side}, {w}x{h}) -> {key}")


def cmd_remove(args, site, catalog, collection):
    key, slot = resolve_slot(catalog, args.slot)
    entry = collection["coins"].get(key)
    if not entry:
        sys.exit(f"{key} is already empty.")
    out_dir = os.path.join(site, "images", *key.split("/"))
    if args.side:
        entry["sides"].pop(args.side, None)
        for name in os.listdir(out_dir) if os.path.isdir(out_dir) else []:
            if name.startswith(args.side + ".") or name.startswith(args.side + "-") or name == args.side + "_files":
                p = os.path.join(out_dir, name)
                shutil.rmtree(p) if os.path.isdir(p) else os.remove(p)
    if not args.side or not entry["sides"]:
        collection["coins"].pop(key)
        shutil.rmtree(out_dir, ignore_errors=True)
    save_json(os.path.join(site, "data", "collection.json"), collection)
    print(f"Removed {key}{' ' + args.side if args.side else ''}")


def cmd_list(args, site, catalog, collection):
    prefix = (args.prefix or "").strip("/")
    for c in catalog["countries"]:
        for d in c["denominations"]:
            for s in d["series"]:
                base = f"{c['id']}/{d['id']}/{s['id']}"
                if prefix and not (base.startswith(prefix) or prefix.startswith(base)):
                    continue
                slots = expand_slots(s)
                have = sum(1 for sl in slots if f"{base}/{sl['id']}" in collection["coins"])
                print(f"{base}  ({s['name']}, {have}/{len(slots)} filled)")
                if prefix.startswith(base) or prefix == base:
                    for sl in slots:
                        mark = "x" if f"{base}/{sl['id']}" in collection["coins"] else " "
                        print(f"   [{mark}] {sl['id']:<24} {sl['label']} {sl['sub']}")


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    p = argparse.ArgumentParser(description="Add scanned coins to the coin book website.")
    p.add_argument("--site", default=os.path.dirname(here), help="the coin-book folder (default: the one this script is in)")
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add", help="put an image in a slot")
    a.add_argument("slot", help="country/denomination/series/slot, e.g. us/cent/wheat/1943-S")
    a.add_argument("image", help="the finished image, ideally the _cropped.png from coin_scan.py cropfinal")
    a.add_argument("--side", choices=SIDES, default="obverse")
    a.add_argument("--grade")
    a.add_argument("--notes")
    a.add_argument("--microscope", help="which microscope or magnification was used")
    a.add_argument("--tile-count", type=int, help="how many photos were stitched")
    a.add_argument("--tiles", action="store_true", help="always make zoom tiles")
    a.add_argument("--no-tiles", action="store_true", help="never make zoom tiles")
    r = sub.add_parser("remove", help="empty a slot (or one side of it)")
    r.add_argument("slot")
    r.add_argument("--side", choices=SIDES)
    l = sub.add_parser("list", help="show series, or the slots in one series")
    l.add_argument("prefix", nargs="?")
    args = p.parse_args()

    site = os.path.abspath(args.site)
    catalog = load_json(os.path.join(site, "data", "catalog.json"))
    col_path = os.path.join(site, "data", "collection.json")
    collection = load_json(col_path) if os.path.exists(col_path) else {"version": 1, "imageBase": "", "coins": {}}
    {"add": cmd_add, "remove": cmd_remove, "list": cmd_list}[args.cmd](args, site, catalog, collection)


if __name__ == "__main__":
    main()
