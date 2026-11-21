// toast.js: a line of text that rises from the bottom of the page and leaves by itself (estate.css's
// .toast; the page has one `<div class="toast" id="toast"></div>`).
//
//   toast(message)                                  shown for 2.6 s
//   toast(message, { action: { label, fn } })       with a button (Undo), shown for 8 s
//   toast(message, { ms, el })                      for another length of time, or in another element
//
// The message is set as text, never as HTML. A new toast replaces the one showing.

/**
 * @typedef {object} ToastOptions
 * @property {{ label: string, fn: () => unknown }} [action]  a button after the message; pressing it puts
 *   the toast away and calls `fn`
 * @property {number} [ms]                how long it shows (default 2600, or 8000 with an action)
 * @property {HTMLElement | null} [el]    the toast's element (default: #toast)
 */

export const TOAST_MS = 2600, TOAST_ACTION_MS = 8000;

/** Each toast element's timer: the one that will put it away. @type {WeakMap<object, ReturnType<typeof setTimeout>>} */
const timers = new WeakMap();

/**
 * Show `message`. Returns the function that puts it away now.
 * @param {string} message
 * @param {ToastOptions} [options]
 * @returns {() => void}
 */
export function toast(message, options = {}) {
  const { action, ms = action ? TOAST_ACTION_MS : TOAST_MS } = options;
  const el = options.el || /** @type {HTMLElement | null} */ (document.querySelector("#toast"));
  if (!el) return () => {};
  const hide = () => { clearTimeout(timers.get(el)); timers.delete(el); el.classList.remove("on"); };
  el.textContent = message;
  el.classList.add("on");
  if (action) {
    const b = document.createElement("button");
    b.textContent = action.label;
    b.onclick = () => { hide(); action.fn(); };
    el.append(b);
  }
  clearTimeout(timers.get(el));
  timers.set(el, setTimeout(hide, ms));
  return hide;
}
