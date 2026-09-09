# epub-fxl-to-pdf

Turn a fixed-layout EPUB into a PDF that actually looks like the book.

Some illustrated books — cookbooks, workbooks, sports and art titles — ship as
*fixed-layout* (FXL) EPUBs. Adobe Digital Editions and the Kobo reader display them
correctly. Calibre's viewer and a number of e-ink readers don't: text lands on top of
photos, call-out boxes lose their background, half the page goes missing. Converting
with `ebook-convert` doesn't rescue it either.

This script renders each page with headless Chromium at the exact size the book asks
for, then prints it to PDF. Text stays vector and searchable, the table of contents
becomes real PDF bookmarks, and the result is readable on any device.

```
python3 fxl2pdf.py book.epub -o book.pdf
```

## Where this came from

I bought two illustrated books from Fnac (Kobo) and neither displayed correctly on my
Boox Note Air5 C — nor in Calibre — while Adobe Digital Editions and the Kobo reader
showed them perfectly. I asked about it on [r/Calibre](https://www.reddit.com/r/Calibre/):

> **[Display issue with Calibre](https://www.reddit.com/r/Calibre/comments/1w8b96h/)**

[u/Zoolef](https://www.reddit.com/user/Zoolef/) pointed at the fixed layout as the likely culprit, which turned out to be
exactly right — though the details were stranger than expected. This repository is what
came out of chasing it down, and the sections below are the full write-up.

## Why fixed-layout breaks

A fixed-layout EPUB is a contract in two parts. The OPF declares
`rendition:layout: pre-paginated`, and every page carries its own

```html
<meta name="viewport" content="width=1647, height=2048">
```

That size is where the layout is true. The reading system is expected to render the page
at that size and scale the whole thing down to the screen.

Many of these books are PDFs that went through a `pdf2htmlEX`-style converter, so the
page is a box of absolutely positioned photos with a text layer on top, placed with
`transform: matrix(0.628...)`, per-glyph negative letter-spacing, and rules like
`.t { white-space: pre; width: 8240px; font-size: 1px }`. Every coordinate assumes the
declared page size.

A reader that ignores the viewport meta renders that box at its real pixel size inside a
much smaller frame. Nothing scales, everything overflows, and the page falls apart.
There is nothing to "reflow" — the text has no flow at all.

## What about Calibre?

Closer than you'd expect:

```
ebook-convert book.epub out.pdf \
  --custom-size 1647x2048 --unit devicepixel \
  --pdf-page-margin-top 0 --pdf-page-margin-bottom 0 \
  --pdf-page-margin-left 0 --pdf-page-margin-right 0 \
  --disable-font-rescaling --embed-all-fonts --expand-css
```

Simple pages come out roughly 95 % right. Complex ones don't: body text falls back to a
serif (`Merged 384 instances of AAAAAA+LiberationSerif` in the log), photos overflow the
text column, and call-out boxes lose their background and become unreadable.

The cause sits upstream of the renderer, and the log announces it:

```
Flattening CSS and remapping font sizes... Source base font size is 34.20000pt
Removing fake margins...
```

That normalisation is precisely what must not happen to a layout built on matrix
transforms and sub-pixel letter-spacing, and there is no switch to turn it off. Calibre's
PDF output runs on QtWebEngine, so the same Chromium underneath — it's the preprocessing
that differs, not the rendering engine.

## Two traps worth knowing about

**Pages named `.html`.** An EPUB declares its pages as `application/xhtml+xml`, but some
publishers still name the files `.html`. Over `file://` Chromium picks its parser from
the extension, so those pages get the HTML parser — where `<span class="_"/>` is *not*
self-closing. The span stays open, swallows the rest of the line, and since these
converters ship a `._ { color: transparent }` rule for inter-word spacers, the text
silently disappears. Copying the pages to `.xhtml` forces the XML parser.

**A wrapper that scales the page.** Some books wrap everything in
`<article style="transform: scale(0.32)">` to shrink the page to its advertised viewport.
A CSS transform doesn't change the *layout* size — and layout size is what Chromium
paginates on when printing. Each page silently gets split across four sheets, of which
only the first is kept. Render at layout size with the ancestors' transforms neutralised
instead; as a bonus the artwork comes out at native resolution rather than downscaled.

On one 224-page book, fixing these two took the extractable text from 30 000 to 215 000
characters.

## Install

```
pip install playwright pypdf
playwright install chromium
```

Optional, for the post-processing flags:

- `ghostscript` and `qpdf` for `--compress`
- `calibre` for `--dedup`

## Usage

```
python3 fxl2pdf.py book.epub -o book.pdf [options]

  --scale FLOAT      multiplier on the PDF page size. A 1647x2048 px page is
                     17 x 21 in at 1.0; the default 0.5 gives a sane 8.6 x 10.7 in
  --dedup            share Type 3 fonts across pages (needs calibre-debug)
  --compress DPI     re-encode images at this DPI (needs ghostscript)
  --quality INT      JPEG quality used by --compress (default 74)
  --viewport WxH     force a size instead of reading the viewport meta
```

A good preset for a 10-inch e-reader:

```
python3 fxl2pdf.py book.epub -o book.pdf --dedup --compress 140 --quality 74
```

### About `--dedup`

Chromium embeds fonts per page with no sharing. A 224-page book comes out with more than
1300 font objects and 25 000 Type 3 glyph procedures — around 18 MB of pure duplication,
and a lot of objects for an e-reader to chew through on every page turn. Calibre already
solves exactly this in its own PDF pipeline, so `--dedup` calls
`calibre.utils.podofo.dedup_type3_fonts` through `calibre-debug` rather than
reimplementing it. On that book: 24 537 glyphs removed, 58 MB down to 38 MB, and
noticeably snappier page turns.

Combined with `--compress 140`, the same book lands at 19 MB with no visible loss on
e-ink.

## Limits

- DRM-protected files are refused outright. The script checks for
  `META-INF/encryption.xml` and stops. Use it on books you can already open.
- Output is a PDF, not an EPUB. For a book whose entire design is absolute positioning,
  a reflowable EPUB would throw away the layout, and an image-based one would throw away
  the vector text. The PDF keeps both.
- Text extraction quality depends on the source. These converters emit one span per
  glyph, so `pdftotext` output can be choppy even though the text is genuinely there and
  searchable in a reader.

## Credits

Written by Claude (Anthropic) while debugging the two books above, with
[@roukmoute](https://github.com/roukmoute). Thanks to [u/Zoolef](https://www.reddit.com/user/Zoolef/) on [r/Calibre](https://www.reddit.com/r/Calibre/) for the
initial pointer.

## License

MIT
