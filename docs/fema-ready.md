# FEMA / Ready.gov — offline collection (#21)

## Selected initial source

Owner approved the English Ready.gov ZIM edition October 2026 on 2026-11-20.

- Stable source ID: `fema-ready`.
- Title: FEMA / Ready.gov.
- Source: [Kiwix ZIM directory](https://download.kiwix.org/zim/zimit/).
- [English archive](https://download.kiwix.org/zim/zimit/ready.gov_en_all_2026-10.zim), directory timestamp 2026-10-15, listed size approximately 1.9 GB.
- Publisher: FEMA / Ready.gov via Kiwix; language English.
- Exact byte count and SHA-256 are not verified. Do not treat the directory's rounded size as an integrity pin.
- Default catalog contains this candidate instead of the former placeholder; an unchanged existing placeholder is upgraded atomically. Edited/deleted records are preserved.

Ready.gov uses the shared ZIM workflow. In **Administration → Sources**, open the Ready.gov card and select **Download**. After verification, review redistribution conditions and archive-specific credits before selecting **Publish**. Open the source from the library homepage and verify offline navigation, embedded documents, and search where supported.

## Earlier PDF collection proposal

The ZIM is the initial priority. Individual official Ready.gov PDFs can be added later if missing or unusable in the archive, using the existing PDF adapter.


## Scope for operator approval

Use existing PDF acquisition and reader, with individual source profiles grouped into a collection named FEMA / Ready.gov and the Emergency Preparedness category. Do not replace the existing planned profile automatically, acquire any files, enable publication, or install a new service.

| Proposed source ID | Title / candidate upstream | Edition indicator |
| --- | --- | --- |
| fema-are-you-ready | [Are You Ready?](https://www.ready.gov/sites/default/files/2021-11/are-you-ready-guide.pdf) | URL directory 2021-11 |
| fema-hazard-sheets | [Hazard Information Sheets — full suite](https://www.ready.gov/sites/default/files/2024-03/ready-gov_full-suite_hazard-info-sheets.pdf) | URL directory 2024-03 |
| fema-family-communication-plan | [Family Emergency Communication Plan](https://www.ready.gov/sites/default/files/2020-03/family-emergency-communication-planning-document.pdf) | URL directory 2020-03 |
| fema-flood | [Flood hazard information sheet](https://www.ready.gov/sites/default/files/2025-01/fema_flood-hazard-info-sheet.pdf) | URL directory 2025-01 |
| fema-power-outage | Existing repository fixture: fixtures/kiwix/content/ready-power-outage-info-sheet.pdf | Compare with current official edition before importing |

The suite and general guide cover the requested core scope including heat, winter weather, power failures and other hazards; shelter and recovery coverage must be checked against the actual edition before acceptance. Start with the general guide, suite and communications guide; keep individual quick-reference sheets as optional additions rather than blindly duplicating the full suite. The URLs above are discovery candidates, not verified pins. Directory dates are not authoritative publication dates.

## Rights, integrity and acquisition gate

Consult the official [Ready.gov publications page](https://www.ready.gov/publications) for reproduction conditions and the document-level credits. Free download does not establish rights for every third-party illustration or mark. Preserve original attribution and notices, do not imply endorsement, and do not use agency branding as custom artwork. Use the existing local PDF card artwork. Publisher metadata: FEMA / Ready.gov; language: English; format: PDF. Do not set a license until applicable reproduction conditions and individual credits are reviewed.

No upstream checksum, confirmed byte size, or stable update cadence has been established here. Before operator-approved acquisition, confirm final URL, response type, actual edition, byte count, rights and any published integrity digest. If there is no publisher hash, record that explicitly; observed SHA-256 is a local integrity pin only. Apply the existing 16 MiB per-document limit; oversized files require a separately reviewed solution. Recheck official publication listings quarterly and after major guidance updates (manual review, no automatic job). Never silently replace a published edition.

## Operator workflow and acceptance

1. Approve the subset of documents and rights review.
2. Add each selected PDF through the existing authenticated source manager with the direct HTTPS URL, title, publisher, provenance and confirmed license where appropriate. Download using existing acquisition controls. For the existing fixture, use the upload flow only after reviewing provenance and rights.
3. Confirm observed size/hash and edition; assign the FEMA / Ready.gov collection and Emergency Preparedness category, plus relevant topic tags.
4. Explicitly publish each verified document. Test library filtering, PDF rendering, images, page-level search, navigation, original download and immediate unpublication.
5. Record the exact acquired pins, results and review date in the issue. No live content or services were acquired by this planning change.

PDF page-level search is available for text-bearing documents; scanned content and interactive PDF form behavior may be limited. This collection is reference material, not a live emergency alert feed.
