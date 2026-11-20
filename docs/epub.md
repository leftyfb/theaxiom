# EPUB books

Add Source accepts a local EPUB file or a direct HTTPS URL ending in .epub. The source title and format are inferred from the filename; available package title, author, publisher and language are extracted, preserving explicit overrides. Installation records the observed byte count and SHA-256, not an independent publisher authenticity guarantee.

Publication is a separate administrator action. Confirm distribution rights or supply a license. Published books appear in the library and metadata search; the reader offers spine chapter navigation, heading-derived contents, adjustable type and original EPUB download. Unpublishing or removing a profile revokes subsequent reader and download requests without removing stored files. Already downloaded bytes cannot be recalled.

## Supported content and limits

- EPUB 2.0 and 3.0, single package, UTF-8 XHTML spine.
- 16 MiB compressed, 64 MiB expanded, 8 MiB per member, 2 MiB XML, 2048 entries, maximum expansion ratio 200:1. Archive paths are strict; parent traversal, encrypted entries, DTDs, scripted chapters and DRM/encryption declarations are rejected. Font-obfuscated books are also unsupported.
- Reader preserves basic semantic text, lists and tables. Manifest-listed PNG, JPEG and GIF images with bounded headers are rendered as typed inline data URLs (2 MiB per image, 4 MiB chapter budget, 16 million pixels). Other image formats, SVG, publisher stylesheets, fonts, external resources and embedded hyperlinks are omitted. Image decoding is performed by the browser; header screening is not full raster re-encoding.
- No JavaScript, third-party reader, CDN or network access is required for reading. Reader HTML is generated from an explicit tag allowlist, with a restrictive content security policy. Original downloads are attachments and still require an external reader that safely handles untrusted EPUB content.
- URL acquisition uses the existing pinned HTTPS transport with independently validated redirects, public addresses only, bounded transfer, and no query strings. Some valid publisher URLs or EPUB packages are therefore intentionally unsupported.

## Acceptance

Upload or download a DRM-free book you are permitted to distribute, confirm extracted metadata, publish, open from the library, exercise chapter and font controls, download the original, then unpublish and verify the reader and download URLs are denied. Automated HTTP tests use local generated EPUB fixtures, including malformed archives and blocked remote images; representative published books and real-browser rendering must be checked separately.
