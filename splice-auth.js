// The server embeds a per-launch token in the page it serves. Same-origin
// fetches carry it so the server can tell this window's requests from those
// of other pages on the machine. Media elements rely on the session cookie.
(() => {
  const token = document.querySelector('meta[name="splice-token"]')?.content;
  if (!token) return;
  const nativeFetch = window.fetch.bind(window);
  window.fetch = (input, init = {}) => {
    const target = new URL(input instanceof Request ? input.url : String(input), location.href);
    if (target.origin !== location.origin) return nativeFetch(input, init);
    const headers = new Headers(init.headers ?? (input instanceof Request ? input.headers : undefined));
    headers.set("X-Splice-Token", token);
    return nativeFetch(input, { ...init, headers });
  };
})();
