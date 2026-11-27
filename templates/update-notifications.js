/* Shared local notification delivery for library and administration pages. */
(() => {
  'use strict';
  if (window.top !== window || window.axiomUpdateNotifications) return;
  window.axiomUpdateNotifications = true;
  const admin = document.currentScript?.dataset.scope === 'admin';
  const endpoint = admin ? '/api/admin/update-notifications' : '/api/update-notifications';
  const POLL = 5000, DISPLAY = 5000;
  let timer, busy = false, current = null, displayTimer, pendingAck = null;
  const style = document.createElement('style');
  style.textContent = '#axiom-update-toast{position:fixed;right:1rem;bottom:1rem;z-index:2147483000;' +
    'max-width:min(28rem,calc(100vw - 2rem));box-sizing:border-box;display:flex;gap:.65rem;align-items:center;' +
    'background:#163c31;color:#fff;padding:.8rem 1rem;border-radius:.7rem;box-shadow:0 4px 20px #0004;' +
    'font:500 1rem/1.4 system-ui,sans-serif;overflow-wrap:anywhere}' +
    '#axiom-update-toast[hidden]{display:none}#axiom-update-toast button{flex:none;border:1px solid #a9d5c2;' +
    'background:transparent;color:white;border-radius:.4rem;min-height:44px;min-width:44px;font:inherit;cursor:pointer}' +
    '#axiom-update-toast button:focus-visible{outline:3px solid white;outline-offset:2px}';
  document.head.append(style);
  const toast = document.createElement('aside');
  toast.id = 'axiom-update-toast';
  toast.hidden = true;
  toast.setAttribute('aria-label', 'Update notification');
  const message = document.createElement('span');
  const live = document.createElement('span');
  live.setAttribute('role', 'status');
  live.setAttribute('aria-live', 'polite');
  live.setAttribute('aria-atomic', 'true');
  Object.assign(live.style, {position:'absolute', width:'1px', height:'1px',
    overflow:'hidden', clipPath:'inset(50%)', whiteSpace:'nowrap'});
  document.body.append(live);
  const dismiss = document.createElement('button');
  dismiss.type = 'button';
  dismiss.textContent = '×';
  dismiss.setAttribute('aria-label', 'Dismiss update notification');
  toast.append(message, dismiss);
  document.body.append(toast);

  async function request(payload) {
    const response = await fetch(endpoint, {
      method: 'POST', credentials: 'same-origin', cache: 'no-store',
      headers: {'Content-Type': 'application/json', ...(admin ? window.axiomCsrfHeaders?.() || {} : {})},
      body: JSON.stringify(payload)
    });
    if (!response.ok) throw new Error('Notification unavailable');
    return response.json();
  }
  function schedule(delay = POLL) {
    clearTimeout(timer);
    if (!document.hidden) timer = setTimeout(poll, delay);
  }
  function hide() {
    clearTimeout(displayTimer);
    toast.hidden = true;
    message.textContent = '';
    live.textContent = '';
  }
  function canDisplay() {
    return !document.hidden && !document.querySelector('dialog[open]');
  }
  function abandon() {
    if (current) { current = null; hide(); }
  }
  function finish() {
    if (!current) return;
    if (!canDisplay()) { abandon(); schedule(); return; }
    const {event_id, claim} = current;
    pendingAck = {action: 'ack', event_id, claim};
    current = null;
    hide();
    schedule(0);
  }
  dismiss.addEventListener('click', finish);
  function show(notification) {
    current = notification;
    toast.hidden = false;
    message.textContent = notification.name + ' updated' + (notification.edition ? ' to ' + notification.edition : '') + '.';
    // Time the complete display from the next rendered frame, not from
    // the network response. No acknowledgement for hidden documents.
    requestAnimationFrame(() => requestAnimationFrame(() => {
      if (current === notification && canDisplay()) {
        live.textContent = message.textContent;
        displayTimer = setTimeout(finish, DISPLAY);
      }
      else { abandon(); schedule(); }
    }));
  }
  async function poll() {
    if (busy || current || document.hidden) return;
    busy = true;
    let next = POLL;
    try {
      if (pendingAck) {
        await request(pendingAck);
        pendingAck = null;
      }
      if (canDisplay()) {
        const started = performance.now();
        const {notification} = await request({action: 'claim'});
        if (notification && canDisplay() &&
            performance.now() - started < ((notification.lease_seconds || 30) * 1000 - DISPLAY - 1000))
          show(notification);
      }
    } catch (_) {
      next = POLL; // persisted claim/ack survives a transient connection failure
    } finally {
      busy = false;
      if (!current) schedule(next);
    }
  }
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) {
      clearTimeout(timer);
      // Abandoned leases become eligible again; never acknowledge a toast
      // that did not get its complete visible display.
      abandon();
    } else schedule(0);
  });
  new MutationObserver(() => {
    if (current && !canDisplay()) { abandon(); schedule(); }
  }).observe(document.body, {subtree: true, attributes: true, attributeFilter: ['open']});
  window.addEventListener('pagehide', () => { clearTimeout(timer); abandon(); });
  window.addEventListener('pageshow', () => schedule(0));
  schedule(0);
})();
