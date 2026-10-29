# Coin Book

A digital coin folder for the microscope scans. Pick a country, then a coin, then a
series (Wheat, Memorial, Bicentennial, Shield...), and you get a folder board with a
hole for every date and mint. Filled holes show the coin; click one to open the
stitched scan in a zoom viewer. Empty holes stay empty, like a real Whitman folder.

It's a plain static site (HTML, CSS, JavaScript, JSON), so GitHub Pages or Cloudflare
Pages can host it with no server.

```
index.html            the page
css/style.css         the look
js/app.js             reads the JSON and draws the album
lib/                  OpenSeadragon, the zoom viewer (BSD licence)
data/catalog.json     which countries, coins, series and slots exist
data/collection.json  which slots are filled, and where their images are
images/               the scans, one folder per slot
tools/add_coin.py     puts a scan into a slot
FORMAT.md             exact format of the JSON files, for the C++ app
```

## Try it on your computer

The page loads JSON files, which browsers block when you double-click `index.html`.
Run a tiny local server instead:

```
cd coin-book
python -m http.server 8000
```

then open http://localhost:8000.

## Add a scanned coin

You need Pillow once: `pip install pillow`.

After `coin_scan.py cropfinal` has made `coin_cropped.png`:

```
python tools\add_coin.py add us/cent/wheat/1943-S coin_cropped.png --side obverse --grade VF-30
python tools\add_coin.py add us/cent/wheat/1943-S other_side_cropped.png --side reverse
```

The slot can be typed the way it's printed (`1943-S`, `"1909-S VDB"`). Other commands:

```
python tools\add_coin.py list                     every series and how full it is
python tools\add_coin.py list us/cent/wheat       every slot in one series
python tools\add_coin.py remove us/cent/wheat/1909 empty a slot
```

The script copies the full image, makes a small thumbnail, makes zoom tiles for
anything over 3000 px, and updates `data/collection.json`. Then publish (below).

The three filled Wheat slots that ship with the site (1909, 1909-S, 1922 No D) are
test images from the scanner trials, labelled "Test image". Remove them with
`add_coin.py remove` or by adding real scans to those slots.

## Add a new series or country

Edit `data/catalog.json`. A whole run of dates is one line:

```json
{ "range": [1938, 2025], "mints": ["", "D", "S"] }
```

See FORMAT.md for the details.

## Publish

**GitHub Pages:** push this folder to a GitHub repository, then in the repo's
Settings > Pages choose "Deploy from a branch", branch `main`, folder `/ (root)`.
To use your own domain, type it in the Custom domain box there, and in Cloudflare
DNS add a `CNAME` record from that name (for example `coins`) to
`<your-github-name>.github.io`.

**Cloudflare Pages** works the same way and keeps everything in Cloudflare:
Workers & Pages > Create > Pages > connect the GitHub repository, with no build command
and output directory `/`. Every push redeploys.

### Image size

A stitched scan can be 50 to 200 MB as PNG. GitHub rejects files over 100 MB and
gets slow past about 1 GB per repository, and Cloudflare Pages allows 25 MB per file.
The zoom tiles are small, so the site itself only ever loads tiles and thumbnails;
the big original is just for the "Open full file" link. Once the collection grows,
put `images/` in a Cloudflare R2 bucket and set `"imageBase"` in
`data/collection.json` to the bucket's public address. Nothing else changes.
