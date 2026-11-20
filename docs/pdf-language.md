# PDF language detection

Acquisition fills a blank language field when a conservative offline detector recognizes a simple PDF Catalog language declaration or sufficiently distinctive text. A manually supplied language is retained. Language codes are stored as metadata and remain editable.

The implementation uses only the Python standard library. It supports classic cross-reference tables, simple Catalog language literals, and bounded plain or Flate-compressed page streams containing simple literal text operands. Text inference currently covers English, Spanish, French, German, Portuguese and Italian. Insufficient or conflicting evidence yields no inferred language. Unsupported modern cross-reference streams, nested dictionaries, custom character mappings, scanned pages without text, and complex text operators may remain unknown. This is deliberately not a full PDF parser or universal language classifier.

A complete parser such as pypdf or an external Poppler tool would broaden extraction but requires dependency and deployment review, including resource isolation for untrusted files. OCR is intentionally excluded: it requires separate language packs, processing budgets and security review. Detection never changes verification requirements or rejects a PDF merely because language is unknown.
