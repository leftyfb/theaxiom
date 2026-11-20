# Adding content sources

The same guide is available offline from Administration → Sources → Content source guide.

## Adding offline content sources

  This guide is stored with The Axiom and remains available without internet access. Finding and downloading new content generally requires connectivity.


  
## 1. Find content

  For Kiwix archives, browse the [Kiwix library](https://library.kiwix.org/) or [official ZIM download directory](https://download.kiwix.org/zim/). Choose your language, edition and archive size. Copy the direct .zim download link rather than the catalog or article page. For PDF documents, use the original publisher, for example [Ready.gov](https://www.ready.gov/), and look for a direct HTTPS PDF link. Check permission to store and distribute the material offline.


  
## 2. Add a source profile

  Choose Add Source, enter a recognizable name and optionally a direct artifact URL, confirm or select the format, optionally describe the collection, and Save source. A stable ID is generated automatically and retained when editing. A normal website URL is not a downloadable archive. Unsupported formats can be recorded but cannot be acquired or read.


  
## 3. Review advanced details

  Publisher, edition and language describe the content. Expected size and SHA-256 are the exact byte count and fingerprint of the intended file. Provenance records where the information came from; license records the permission to use it. Use trustworthy publisher metadata where available. A checksum calculated from a downloaded file can identify those bytes later, but does not independently prove their origin. Saving metadata does not verify it.


  
## 4. Acquire and verify

  **ZIM:** The existing verified Wikipedia archive is supported. New ZIM profiles currently require a matching trusted acquisition manifest; arbitrary remote downloads are not yet enabled.


  **PDF:** Use Download from URL for a configured HTTPS PDF link (up to three independently validated HTTPS redirects; query strings and private addresses remain unsupported), or Upload PDF to choose a file from your computer. Both support up to 16 MiB, compute missing size and SHA-256, and record provenance. A URL is optional for uploads. Review rights and enter a rights note before publishing. Local or explicitly configured NFS storage is supported; the expected mount target and export must match.


  
## 5. Publish and read offline

  After matching content is verified and its reader is ready, use Publish on the source card. Open the published collection from The Axiom homepage, even without internet connectivity. PDFs use the browser PDF viewer; PDF full-text search is not yet supported.


  
## 6. Edit or remove

  Edit display details without changing the stable ID. Unpublish before changing the artifact identity or removing a published profile. Removing a profile does not delete installed files. Changing the URL, size or checksum does not transfer verification to a different artifact. Active or unresolved acquisitions may block editing.
