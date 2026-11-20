# Offline PDF reader (#74)

Install the official Mozilla PDF.js legacy generic distribution 6.5.438 at
`/opt/pdfjs/6.5.438` and link `/opt/pdfjs/current` to it. Use
`python3 scripts/vendor_pdfjs.py /opt/pdfjs/6.5.438` as an administrator on a
connected preparation host, then transfer the verified directory if needed.
Keep the distribution and license notices root-owned and not writable by the
application service account. No installation-time changes to content storage
are needed. No CDN or runtime internet access is used.

The application exposes only renderer assets through the existing /api/ proxy.
It does not expose the upstream generic viewer HTML, arbitrary file URLs,
PDF annotations/actions, scripting layer, XFA, or document-defined links.
The application owns the toolbar and renders pages using PDF.js directly.
The authorized PDF endpoint is unchanged and checked before reader and document
delivery. A restrictive reader CSP limits resources to the same origin; PDFs
are loaded into the renderer as bytes, not navigated as active HTML.

Features: page navigation, fit and zoom, lazy thumbnails, text search by
matching page (not OCR or highlighted hits), print (up to 100 pages, bounded
resolution), download and keyboard navigation. The download fallback remains
available when the renderer is unavailable. Already downloaded bytes cannot
be revoked. Browser/Pi acceptance must verify real rendering, search, printing,
offline requests and resource use. PDF.js is Apache-2.0; preserve upstream
license and notices, including bundled fonts. Verify actual host installation
and browser compatibility before acceptance. Track dependency inventory in #79.
