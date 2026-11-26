(() => {
  const frame = document.getElementById('content_iframe');
  if (!frame) return;

  // Kiwix auto-hides its toolbar on narrow screens by repeatedly setting
  // position: fixed and top: -100% while the article scrolls down. Keep the
  // toolbar in the viewer's flex layout, above the independently scrolling
  // content iframe. CSS takes precedence over those inline scroll updates.
  const toolbarStyle = document.createElement('style');
  toolbarStyle.textContent = `
    @media screen {
      #kiwixtoolbarwrapper { flex-shrink: 0; }
      #kiwixtoolbar {
        position: static !important;
        top: auto !important;
        transition: none !important;
      }
      #content_iframe { min-height: 0; }
    }
  `;
  document.head.append(toolbarStyle);

  const viewer = window.location.pathname;
  const content = viewer.replace(/viewer$/, 'content/');
  const bookPath = /^[a-z0-9-]+\.[a-f0-9]{64}\//;

  function followArticle(event) {
    if (event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
    const link = event.target.closest?.('a[href]');
    const href = link?.getAttribute('href');
    if (!href || href.startsWith('#') || link.hasAttribute('download')) return;
    const target = link.getAttribute('target');
    if (target && !['_self', '_parent', '_top'].includes(target.toLowerCase())) return;

    const url = new URL(link.href, link.ownerDocument.baseURI);
    if (url.origin !== window.location.origin || url.search) return;
    let article;
    if (url.pathname.startsWith(content)) {
      article = url.pathname.slice(content.length) + url.hash;
    } else if (url.pathname === viewer) {
      article = url.hash.slice(1);
    }
    if (!article || !bookPath.test(article)) return;

    // Handle local article clicks before the archive or Kiwix click handler.
    event.preventDefault();
    event.stopImmediatePropagation();
    window.location.hash = article;
  }

  function bindArticleClicks() {
    const doc = frame.contentDocument;
    doc?.addEventListener('click', followArticle, true);
  }
  frame.addEventListener('load', bindArticleClicks);
  bindArticleClicks();
})();
