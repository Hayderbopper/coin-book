# Coin Book data format (version 1)

This is the contract between the scanner app and the website. Anything that writes
these two files and the image files the same way will show up in the album: the
Python helper `tools/add_coin.py` today, the C++ app later.

The site is fully static. It only reads `data/catalog.json`, `data/collection.json`
and the image files, so publishing a new coin is "write files, then upload them".

## 1. `data/catalog.json`: which slots exist

This is the empty folder: countries, denominations, series and the slots in each.
You edit it by hand when you start collecting something new. The scanner never
needs to change it.

```json
{
  "version": 1,
  "title": "Coin Book",
  "countries": [
    {
      "id": "us",
      "name": "United States",
      "denominations": [
        {
          "id": "cent",
          "name": "Cent",
          "nickname": "Penny",
          "series": [
            {
              "id": "wheat",
              "name": "Lincoln Wheat",
              "dates": "1909–1958",
              "diameter_mm": 19.05,
              "description": "Shown on the folder cover and board.",
              "slots": [
                "1909",
                "1909-S VDB",
                { "range": [1911, 1920], "mints": ["", "D", "S"] },
                { "id": "1922-no-d", "label": "1922", "sub": "No D" }
              ]
            }
          ]
        }
      ]
    }
  ]
}
```

`id`s appear in web addresses and folder names, so keep them lowercase with
hyphens, and don't rename one after coins are filed under it.

A `slots` entry can be any of three forms. They are listed in folder order.

| Form | Example | Becomes |
| --- | --- | --- |
| Text | `"1909-S VDB"` | slot id `1909-s-vdb`, label `1909-S VDB` |
| Range | `{ "range": [2010, 2012], "mints": ["", "D"] }` | `2010`, `2010-D`, `2011`, `2011-D`, `2012`, `2012-D` |
| Object | `{ "id": "2009-d-presidency", "label": "2009-D", "sub": "Presidency" }` | that exact id; `sub` is a second line under the label |

Add `"variety": true` to an object slot to mark it as a die variety or other
sub-type (for example `{ "id": "1955-doubled-die-obverse", "label": "1955", "sub": "Doubled Die Obverse", "variety": true }`).
The board gives it a gold ring and a "Varieties" button that hides or shows them.

A slot id is made by lowercasing the label and turning every run of other
characters into one `-` (so `1909-S VDB` becomes `1909-s-vdb`). `mints: [""]` means
no mint mark.

`diameter_mm` is optional. When it's there, the viewer shows the scan's detail as
pixels per millimetre.

## 2. `data/collection.json`: which slots are filled

```json
{
  "version": 1,
  "imageBase": "",
  "coins": {
    "us/cent/wheat/1909-s": {
      "sides": {
        "obverse": {
          "image": "images/us/cent/wheat/1909-s/obverse.png",
          "thumb": "images/us/cent/wheat/1909-s/obverse-thumb.webp",
          "tiles": "images/us/cent/wheat/1909-s/obverse.dzi",
          "width": 8240,
          "height": 8240
        },
        "reverse": { "image": "…", "thumb": "…", "width": 8100, "height": 8100 }
      },
      "grade": "VF-20",
      "notes": "Light toning on the reverse.",
      "scan": { "microscope": "1000x USB, 40x", "tiles": 36 },
      "added": "2026-11-02",
      "updated": "2026-11-02"
    }
  }
}
```

- **Key**: `country/denomination/series/slot`, all ids from the catalog. A key that
  doesn't match a slot is ignored by the site.
- **sides**: any of `obverse`, `reverse`, `edge`. Only one side is needed.
  - `image` (required): the full-resolution picture. PNG with a transparent
    background is best; JPEG and WebP work too.
  - `thumb` (recommended): about 480 px, shown in the folder hole. Falls back to
    `image`, which is slow for big scans.
  - `tiles` (optional): a Deep Zoom `.dzi` file. When present the viewer streams
    tiles instead of downloading the whole image, which matters above roughly
    3000 px. Tiles may be `webp`, `png` or `jpg`.
  - `width`, `height` (recommended): pixel size of `image`.
- **grade**, **notes**, **scan**, **added**, **updated**: optional, shown in the viewer.
  Dates are `YYYY-MM-DD`; the home page sorts "Recently added" by `updated`, then `added`.
- **sample**: `true` marks a placeholder. The site labels it "Test image".
  `add_coin.py` clears it when you add a real scan.

### Where images live

Paths are relative to the site unless they start with `http`. If `imageBase` is set,
it is put in front of every relative path, so moving all images to a different host
(for example Cloudflare R2 at `https://images.example.com/`) only means changing that
one line.

Recommended layout, which `add_coin.py` uses:

```
images/<country>/<denomination>/<series>/<slot>/
    obverse.png
    obverse-thumb.webp
    obverse.dzi
    obverse_files/<level>/<col>_<row>.webp
```

## 3. Rules for an exporter

1. Read `catalog.json`, check the slot exists, and compute the key.
2. Write the image, the thumbnail and, for big images, the tiles.
3. Read `collection.json` again just before writing, change only that coin's entry,
   and write the whole file back (write to a temp file, then rename, so a crash can't
   leave half a file).
4. Upload the changed files (git push for GitHub Pages, or an upload to R2 or
   Cloudflare Pages).

Deep Zoom tiles: tile size 254, overlap 1, level `n` is the image halved
`max_level - n` times where `max_level = ceil(log2(max(width, height)))`. The `.dzi`
file is:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<Image xmlns="http://schemas.microsoft.com/deepzoom/2008" Format="webp" Overlap="1" TileSize="254">
  <Size Width="8240" Height="8240"/>
</Image>
```

In C++, libvips (`vips dzsave`) writes exactly this layout, so the desktop app can
call it instead of tiling by hand.
