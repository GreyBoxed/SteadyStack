// Tabs that reload the page keep the reader where they were.
//
// A tab here is an ordinary link to the same page with a different query --
// server-rendered, no client state -- so the browser starts the new page at
// the top. A link inside [data-keep-scroll] remembers the position for this
// path, and the next page on the same path restores it once.
(function () {
  var key = "keep-scroll:" + location.pathname;

  try {
    var saved = sessionStorage.getItem(key);
    if (saved !== null) {
      sessionStorage.removeItem(key);
      window.scrollTo(0, parseInt(saved, 10) || 0);
    }
  } catch (e) {
    // Storage unavailable: the page simply opens at the top.
  }

  document.addEventListener("click", function (event) {
    if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) {
      return;
    }
    var link = event.target.closest("[data-keep-scroll] a[href]");
    if (!link || new URL(link.href, location.href).pathname !== location.pathname) {
      return;
    }
    try {
      sessionStorage.setItem(key, String(window.scrollY));
    } catch (e) {}
  });
})();
