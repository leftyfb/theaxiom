# Structured-data sources

The generic JSON and CSV adapter uses the existing managed archive acquisition,
checksum, resumable HTTP transport, staged replacement, rollback, storage and
publication lifecycle. It does not generate source-specific code or bundle
publisher data.

Add a source in Admin → Sources, select JSON data or CSV data, and enter the
direct HTTPS URL, provenance, edition and appropriate rights note. A publisher
SHA-256 or expected size can be pinned when available. Download and publish
are separate operations. A locally calculated digest identifies the acquired
bytes but is not a publisher signature.

JSON expects an array of objects at the top level, or at the configured
dot-separated record path. CSV expects UTF-8 comma-separated text with a unique
header row. Columns can be selected in display order with a comma-separated
list. JSON nested scalar columns may use dot-separated paths. If omitted, the
adapter discovers top-level scalar fields or CSV headers. A record title field
is optional. Only displayed scalar fields are searchable. Nested objects and
arrays are not executed, fetched, or rendered as HTML.

The first version is intentionally bounded to 16 MiB, 50,000 records, 48
displayed columns, one million displayed cells and 4,096 characters per cell.
Large JSONL, compressed bulk exports, multi-gigabyte datasets, and arbitrary
transformations are not supported. Data is validated before atomic installation.
The reader renders escaped plain text in responsive cards or a scrollable table,
with local search, 20-record pages, source attribution, and digest-bound record
links; global search uses the same records and respects publication and search
opt-out. No browser network access to upstream data is needed. Record links to
a replaced dataset stop resolving rather than pointing to unrelated records.

Update discovery is manual in this version. To prepare a replacement, edit the
candidate URL or pin an independently verified changed SHA-256/size and use
the existing staged replacement controls. Resume requires a stable publisher
validator, and rollback retains the previous verified snapshot. No unattended
update policy is offered for these formats until trusted update discovery is
implemented. A source with an unchanged URL and no changed pin is not
automatically treated as a new edition.

Optional numeric range labels use a JSON array `["field","label",[[0,10,"Low"],[10,20,"Medium"]]]`.
Intervals are ordered, nonoverlapping, lower-inclusive and upper-exclusive.
The source field must be an explicitly displayed column. Unmatched values
produce an empty label; the derived column is searchable and rendered as text.

HearHam is a preconfigured opt-in source using the publisher's live JSON API.
On 2026-11-27 the export contained 24,813 records (10,308,065 bytes; SHA-256
`135828c4deb505cecb311272e325b51c4c140a6d24d088b812f711b3c4025d5c`).
The default deliberately does not pin that mutable snapshot. The source
includes nominal band labels based on frequency in Hz; these do not establish
local transmission authorization. The endpoint did not advertise an ETag or
Last-Modified validator, so discovery is manual. See the publisher's
[application-use permission](https://hearham.com/repeaters) and
[terms](https://hearham.com/terms). No dataset is bundled or downloaded
automatically.
