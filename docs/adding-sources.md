# Adding content sources

The same guide is available offline from Administration → Sources → Content source guide.

## Adding offline content sources

  This guide is stored with The Axiom and remains available without internet access. Finding and downloading new content generally requires connectivity.


  
## 1. Find content

  For Kiwix archives, browse the [Kiwix library](https://library.kiwix.org/) or [official ZIM download directory](https://download.kiwix.org/zim/). Choose your language, edition and archive size. Copy the direct .zim download link rather than the catalog or article page. For PDF documents, use the original publisher, for example [Ready.gov](https://www.ready.gov/), and look for a direct HTTPS PDF link. Check permission to store and distribute the material offline.


  
## 2. Add a source profile

  Choose Add Source and select a local PDF to populate an editable title from its filename and install it in one operation. When the title is not edited, a meaningful simple embedded PDF Info title takes precedence during installation; compressed, encrypted, complex or unsupported metadata falls back to the filename. Alternatively, enter a recognizable name and optionally a direct artifact URL, confirm or select the format, optionally describe the collection, and Save source. A stable ID is generated automatically and retained when editing. A normal website URL is not a downloadable archive. Unsupported formats can be recorded but cannot be acquired or read.


  
## 3. Review advanced details

  Publisher, edition and language describe the content. Expected size and SHA-256 are the exact byte count and fingerprint of the intended file. Provenance records where the information came from; license records the permission to use it. Use trustworthy publisher metadata where available. A checksum calculated from a downloaded file can identify those bytes later, but does not independently prove their origin. Saving metadata does not verify it.


  
## 4. Acquire and verify

  **ZIM:** Wikipedia, Ready.gov and custom archives use the same ZIM controls. For URL acquisition, configure an eligible HTTPS ZIM profile and use its dedicated download/verification workflow. Publication requires a verified installed artifact and the shared Kiwix reader; merely saving a URL, checksum or shipped seed does not establish trust. The legacy action API remains disabled. Existing archives and bookmarked reader routes are preserved during upgrades; content is never downloaded or published simply because a seed is present.


  **PDF:** Use Download from URL for a configured HTTPS PDF link (up to three independently validated HTTPS redirects; query strings and private addresses remain unsupported), or Upload PDF to choose a file from your computer. Both support up to 16 MiB, compute missing size and SHA-256, and record provenance. A URL is optional for uploads. When publishing a PDF without a documented license, confirm you have permission to make it available to library users. This acknowledgement is not independent verification of publisher rights. Local or explicitly configured NFS storage is supported; the expected mount target and export must match.


  
## 5. Publish and read offline

  After matching content is verified and its reader is ready, use Publish on the source card. Open the published collection from The Axiom homepage, even without internet connectivity. PDFs use the browser PDF viewer; PDF full-text search is not yet supported.


  
## 6. Edit or remove

  Edit display details without changing the stable ID. Unpublish before changing the artifact identity. Removing a published profile automatically unpublishes it and does not delete installed files. If removal fails after unpublishing, the source stays unpublished; reload and retry removal. Changing the URL, size or checksum does not transfer verification to a different artifact. Active or unresolved acquisitions may block editing.


## 7. Organize sources

Open Organize sources on Administration → Sources. Add predefined categories once, or manage your own categories deliberately. Create collections for curated sets and tags for flexible keywords. Select one or more source cards, choose a grouping type and group, and add or remove the selected memberships. A source can belong to many groups without duplicating content. Deleting a group never removes source profiles or installed files. Use Create tag & assign to create and assign a new tag in the same interaction.


Public library visitors can filter published sources by category, collection and tag, including combinations. Search respects selected groups; PDFs currently match title and description only, not full document text. Groups without published sources are not shown.


## 8. Filter administration sources

The Sources page defaults to published first, verified downloaded second, and all other sources last, alphabetical within each group. Combine independent publication and download filters with title, format and grouping filters. Unknown/unverified storage status is distinct from confirmed absence. Refresh order explicitly after live status changes; filter selections persist between reloads. Bulk selection includes visible cards only when using Select visible sources; the selected count identifies hidden selections before assignment.

## 9. Export ZIM verification records

In Administration → Sources, select installed, verified ZIMs and choose **Export ZIM verification manifest**. The JSON records the local archive filename, byte count, recorded SHA-256, edition, and catalog provenance and license notes. It is a metadata export. Archive copying and import verification are separate steps.

Open **Library selections** and choose the manifest JSON to check its shape and compare it with local source IDs and recorded checksums. The preview leaves file verification, copying, and import to separate steps.
