#!/usr/bin/env python3
"""fxl2pdf - turn a fixed-layout EPUB into a faithful PDF.

A fixed-layout ("pre-paginated") EPUB describes its pages at one fixed size, declared
per page in a <meta name="viewport"> tag, and relies on the reading system to scale the
whole page down to the screen. Adobe Digital Editions and the Kobo reader honour that
contract. Calibre's viewer and a number of e-ink readers don't: the page box keeps its
real size and the layout falls apart.

This script does the scaling itself. Each page is rendered by headless Chromium at the
exact viewport the book declares, then *printed* to PDF - not screenshotted - so the
text stays vector and searchable. Pages are merged in spine order and the table of
contents is rebuilt as real PDF bookmarks.

Requirements:
    pip install playwright pypdf
    playwright install chromium
    optional: ghostscript + qpdf (--compress), calibre (--dedup)

Usage:
    python3 fxl2pdf.py book.epub -o book.pdf
    python3 fxl2pdf.py book.epub -o book.pdf --dedup --compress 140 --quality 74
"""

import argparse
import asyncio
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

XHTML = "{http://www.w3.org/1999/xhtml}"
OPS = "{http://www.idpf.org/2007/ops}"

# Web fonts are applied asynchronously and images may still be decoding. Capturing
# before both are settled gives wrong text metrics and blank pictures.
WAIT_JS = """() => Promise.all([
  document.fonts ? document.fonts.ready : Promise.resolve(),
  ...Array.from(document.images).map(i => i.complete ? Promise.resolve()
      : new Promise(r => { i.onload = i.onerror = r; }))
])"""

# The page box is the largest block in the document. Measuring it two ways - layout
# size vs. size on screen - reveals whether an ancestor scales the page down.
MEASURE_JS = """() => {
  const cands = [...document.querySelectorAll('.pf, #page-container, body, body > *')];
  let best = null, area = 0;
  for (const e of cands) { const a = e.offsetWidth * e.offsetHeight;
                           if (a > area) { area = a; best = e; } }
  if (!best) return null;
  const r = best.getBoundingClientRect();
  return {layout: [best.offsetWidth, best.offsetHeight],
          visual: [Math.round(r.width), Math.round(r.height)]};
}"""

UNSCALE_JS = """() => {
  const cands = [...document.querySelectorAll('.pf, #page-container, body, body > *')];
  let best = null, area = 0;
  for (const e of cands) { const a = e.offsetWidth * e.offsetHeight;
                           if (a > area) { area = a; best = e; } }
  if (!best) return;
  for (let p = best.parentElement; p; p = p.parentElement) {
    p.style.setProperty('transform', 'none', 'important');
    p.style.setProperty('width', 'auto', 'important');
    p.style.setProperty('height', 'auto', 'important');
  }
  document.documentElement.style.margin = '0';
  document.body.style.margin = '0';
}"""


# --------------------------------------------------------------------------- EPUB

def opf_path(root: Path) -> Path:
    c = ET.fromstring((root / "META-INF" / "container.xml").read_text(encoding="utf-8"))
    rf = c.find(".//{urn:oasis:names:tc:opendocument:xmlns:container}rootfile")
    return root / rf.get("full-path")


def read_spine(opf: Path):
    """Return (page hrefs in reading order, href -> page index, raw OPF text)."""
    src = opf.read_text(encoding="utf-8")
    manifest = dict(re.findall(r'<item\s+href="([^"]+)"\s+id="([^"]+)"', src))
    id2href = {i: h for h, i in manifest.items()}
    order = [id2href[i] for i in re.findall(r'<itemref\s+idref="([^"]+)"', src)
             if i in id2href]
    href2idx = {}
    for i, h in enumerate(order):
        href2idx.setdefault(h, i)
    return order, href2idx, src


def detect_viewport(base: Path, hrefs, fallback=(1200, 1600)):
    """Read the per-page viewport meta: the size at which the layout is correct."""
    sizes = {}
    for h in hrefs[:30]:
        try:
            head = (base / h).read_text(encoding="utf-8", errors="replace")[:4000]
        except OSError:
            continue
        m = (re.search(r'name="viewport"[^>]*content="width=(\d+),\s*height=(\d+)"', head)
             or re.search(r'content="width=(\d+),\s*height=(\d+)"[^>]*name="viewport"', head))
        if m:
            key = (int(m.group(1)), int(m.group(2)))
            sizes[key] = sizes.get(key, 0) + 1
    if not sizes:
        print(f"  ! no viewport meta found, falling back to {fallback}")
        return fallback
    return max(sizes.items(), key=lambda kv: kv[1])[0]


def force_xhtml(base: Path, hrefs):
    """Duplicate .html pages as .xhtml so Chromium uses the XML parser.

    An EPUB declares its pages as application/xhtml+xml, but some publishers name the
    files .html. Over file:// Chromium picks its parser from the extension, so those
    pages get the HTML parser - where `<span class="_"/>` is NOT self-closing. The span
    stays open and swallows the rest of the line, and since these converters ship a
    `._ { color: transparent }` rule for inter-word spacers, the text simply vanishes.
    """
    out = []
    for h in hrefs:
        src = base / h
        if src.suffix.lower() in (".xhtml", ".xht") or not src.exists():
            out.append(h)
            continue
        dst = src.with_suffix(".xhtml")
        if not dst.exists():
            shutil.copyfile(src, dst)
        out.append(str(dst.relative_to(base)).replace("\\", "/"))
    if out != list(hrefs):
        print("  copied .html pages to .xhtml (XML parser)")
    return out


# ------------------------------------------------------------------------ render

async def render(base: Path, hrefs, size, outdir: Path, verbose=True):
    from playwright.async_api import async_playwright

    w, h = size
    outdir.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as p:
        browser = await p.chromium.launch()

        # Some books wrap the page in <article style="transform: scale(0.32)"> to shrink
        # it to the size advertised in the viewport meta. A CSS transform does not change
        # the *layout* size, and layout size is what Chromium paginates on when printing:
        # it would silently split each page across several sheets, of which we keep only
        # the first. So render at layout size with the ancestors' transforms neutralised.
        # As a bonus the artwork comes out at native resolution instead of downscaled.
        probe = await browser.new_page(viewport={"width": w, "height": h})
        await probe.goto((base / hrefs[0]).as_uri(), wait_until="load")
        try:
            await probe.evaluate(WAIT_JS)
        except Exception:
            pass
        m = await probe.evaluate(MEASURE_JS)
        await probe.close()

        unscale = False
        if m and m["visual"][0] and m["layout"][0] / m["visual"][0] > 1.02:
            w, h = m["layout"]
            unscale = True
            print(f"  book scales its own pages: rendering at {w}x{h} (layout size)")

        page = await browser.new_page(viewport={"width": w, "height": h})
        for n, href in enumerate(hrefs, 1):
            dest = outdir / f"p{n:04d}.pdf"
            if dest.exists() and dest.stat().st_size:
                continue
            await page.goto((base / href).as_uri(), wait_until="load")
            try:
                await page.evaluate(WAIT_JS)
            except Exception:
                pass
            if unscale:
                await page.evaluate(UNSCALE_JS)
            await page.wait_for_timeout(100)
            dest.write_bytes(await page.pdf(
                width=f"{w}px", height=f"{h}px", print_background=True,
                margin={"top": "0", "right": "0", "bottom": "0", "left": "0"},
                page_ranges="1"))
            if verbose and (n % 20 == 0 or n == len(hrefs)):
                print(f"  {n}/{len(hrefs)}")
        await browser.close()


# ------------------------------------------------------------------------- merge

def outline_from_nav(base: Path, opf_src: str, href2idx, writer) -> int:
    """Rebuild PDF bookmarks from the EPUB 3 navigation document."""
    m = (re.search(r'<item\s+href="([^"]+)"[^>]*properties="[^"]*\bnav\b', opf_src)
         or re.search(r'properties="[^"]*\bnav\b[^"]*"[^>]*href="([^"]+)"', opf_src))
    if not m or not (base / m.group(1)).exists():
        return 0

    root = ET.fromstring((base / m.group(1)).read_text(encoding="utf-8"))
    nav = next((n for n in root.iter(XHTML + "nav")
                if n.get("id") == "toc" or n.get(OPS + "type") == "toc"), None)
    if nav is None:
        return 0

    count = 0

    def walk(ol, parent):
        nonlocal count
        for li in ol.findall(XHTML + "li"):
            a = li.find(XHTML + "a")
            if a is None:
                continue
            title = " ".join("".join(a.itertext()).split())
            idx = href2idx.get((a.get("href") or "").split("#")[0])
            if idx is None or not title:
                continue
            item = writer.add_outline_item(title, idx, parent=parent)
            count += 1
            sub = li.find(XHTML + "ol")
            if sub is not None:
                walk(sub, item)

    top = nav.find(XHTML + "ol")
    if top is not None:
        walk(top, None)
    return count


def merge(pages, opf_src: str, base: Path, href2idx, out: Path, scale: float):
    from pypdf import PdfReader, PdfWriter

    writer = PdfWriter()
    for f in pages:
        pg = PdfReader(str(f)).pages[0]
        if scale != 1.0:
            pg.scale_by(scale)
        writer.add_page(pg)

    print(f"  bookmarks: {outline_from_nav(base, opf_src, href2idx, writer)}")

    def dc(tag):
        m = re.search(rf"<dc:{tag}[^>]*>(.*?)</dc:{tag}>", opf_src, re.S)
        return re.sub(r"<[^>]+>", "", m.group(1)).strip() if m else ""

    writer.add_metadata({k: v for k, v in {
        "/Title": dc("title"),
        "/Author": dc("creator"),
        "/Creator": "fxl2pdf (Chromium render at native viewport)",
    }.items() if v})
    with open(out, "wb") as fh:
        writer.write(fh)


# ---------------------------------------------------------------- post-processing

DEDUP_CODE = r"""
import sys
from calibre.utils.podofo import get_podofo, dedup_type3_fonts, remove_unused_fonts
from calibre.ebooks.pdf.html_writer import merge_fonts
from calibre.utils.logging import default_log as log
src, dst = sys.argv[-2], sys.argv[-1]
doc = get_podofo().PDFDoc(); doc.open(src)
remove_unused_fonts(doc)
merge_fonts(doc, log)
print('duplicated Type 3 glyphs removed:', dedup_type3_fonts(doc))
print('duplicate images removed:', doc.dedup_images())
remove_unused_fonts(doc)
doc.save(dst)
"""


def dedup_type3(src: Path, dst: Path) -> Path:
    """Share Type 3 fonts across pages, using Calibre's own code.

    Chromium embeds fonts per page with no sharing: a 224-page book ends up with 1300+
    font objects and 25000 glyph procedures - some 18 MB of duplicates, and a lot of
    objects for an e-reader to chew through. Calibre already solves exactly this in its
    PDF output pipeline, so call it rather than reimplement it.
    """
    if not shutil.which("calibre-debug"):
        print("  ! calibre-debug not found, skipping dedup")
        return src
    r = subprocess.run(["calibre-debug", "-c", DEDUP_CODE, str(src), str(dst)],
                       capture_output=True, text=True,
                       env={**os.environ, "QT_QPA_PLATFORM": "offscreen"})
    if r.returncode or not dst.exists():
        tail = (r.stderr or r.stdout).strip().splitlines()[-1:] or ["unknown error"]
        print("  ! dedup failed:", tail[0])
        return src
    for line in r.stdout.strip().splitlines():
        print("  " + line)
    return dst


def compress(src: Path, dst: Path, dpi: int, quality: int) -> Path:
    """Re-encode the images. Text is vector and untouched.

    PassThroughJPEGImages must be off or Ghostscript copies the original JPEG through,
    and the downsample threshold must be 1.0 or it refuses to resample anything less
    than 1.5x above the target.
    """
    if not shutil.which("gs"):
        print("  ! ghostscript not found, skipping compression")
        shutil.copy(src, dst)
        return dst
    tmp = dst.with_suffix(".gs.pdf")
    subprocess.run([
        "gs", "-q", "-dNOPAUSE", "-dBATCH", "-sDEVICE=pdfwrite",
        "-dCompatibilityLevel=1.5",
        "-dPassThroughJPEGImages=false", "-dDetectDuplicateImages=true",
        "-dAutoFilterColorImages=false", "-dColorImageFilter=/DCTEncode",
        "-dAutoFilterGrayImages=false", "-dGrayImageFilter=/DCTEncode",
        "-dDownsampleColorImages=true", "-dColorImageDownsampleType=/Bicubic",
        f"-dColorImageResolution={dpi}", "-dColorImageDownsampleThreshold=1.0",
        "-dDownsampleGrayImages=true", "-dGrayImageDownsampleType=/Bicubic",
        f"-dGrayImageResolution={dpi}", "-dGrayImageDownsampleThreshold=1.0",
        f"-dJPEGQ={quality}", "-dSubsetFonts=true", "-dCompressFonts=true",
        f"-sOutputFile={tmp}", str(src)], check=True)
    if shutil.which("qpdf"):
        subprocess.run(["qpdf", "--linearize", "--object-streams=generate",
                        str(tmp), str(dst)], check=False)
        tmp.unlink(missing_ok=True)
    else:
        tmp.replace(dst)
    return dst


# -------------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description="Fixed-layout EPUB -> faithful PDF")
    ap.add_argument("epub", type=Path)
    ap.add_argument("-o", "--out", type=Path, required=True)
    ap.add_argument("--scale", type=float, default=0.5,
                    help="multiplier on the PDF page size; 0.5 turns a 1647x2048 px "
                         "page into a sane 8.6 x 10.7 in one (default: 0.5)")
    ap.add_argument("--dedup", action="store_true",
                    help="share Type 3 fonts across pages (needs calibre-debug)")
    ap.add_argument("--compress", type=int, metavar="DPI",
                    help="re-encode images at this DPI (needs ghostscript)")
    ap.add_argument("--quality", type=int, default=74,
                    help="JPEG quality used by --compress (default: 74)")
    ap.add_argument("--viewport", help="force WIDTHxHEIGHT instead of the viewport meta")
    args = ap.parse_args()

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        book = tmp / "book"
        with zipfile.ZipFile(args.epub) as z:
            if any("encryption.xml" in n for n in z.namelist()):
                sys.exit("This file is encrypted (DRM). Nothing to do here.")
            z.extractall(book)

        opf = opf_path(book)
        base = opf.parent
        hrefs, href2idx, opf_src = read_spine(opf)
        if "pre-paginated" not in opf_src:
            print("  ! this book does not declare itself pre-paginated; "
                  "the result may be wrong")

        size = (tuple(int(x) for x in args.viewport.lower().split("x"))
                if args.viewport else detect_viewport(base, hrefs))
        print(f"{len(hrefs)} pages, viewport {size[0]}x{size[1]}")

        asyncio.run(render(base, force_xhtml(base, hrefs), size, tmp / "pages"))

        pdf = tmp / "merged.pdf"
        merge(sorted((tmp / "pages").glob("p*.pdf")), opf_src, base, href2idx,
              pdf, args.scale)

        if args.dedup:
            pdf = dedup_type3(pdf, tmp / "dedup.pdf")

        args.out.parent.mkdir(parents=True, exist_ok=True)
        if args.compress:
            compress(pdf, args.out, args.compress, args.quality)
        else:
            shutil.copy(pdf, args.out)

    print(f"done -> {args.out} ({args.out.stat().st_size / 1048576:.1f} MB)")


if __name__ == "__main__":
    main()
