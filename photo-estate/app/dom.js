// dom.js: what every page uses to find elements and to write text into HTML. A native ES module, like
// everything in app/*.js: a page imports it (`import { $, esc } from "/app/dom.js"`), nothing is bundled.
//
//   $(selector, root = document)        the first element that matches, or null
//   $$(selector, root = document)       every element that matches, as an array
//   esc(value)                          text made safe inside HTML and inside a quoted attribute
//   on(type, fn, target = document)     listen; returns the function that stops listening
//   MARK                                the icon's picture, shown where there is nothing to show

/**
 * The first element under `root` that matches `selector`, or null.
 * @template {Element} [T=HTMLElement]
 * @param {string} selector
 * @param {ParentNode} [root]
 * @returns {T | null}
 */
export const $ = (selector, root = document) => root.querySelector(selector);

/**
 * Every element under `root` that matches `selector`.
 * @template {Element} [T=HTMLElement]
 * @param {string} selector
 * @param {ParentNode} [root]
 * @returns {T[]}
 */
export const $$ = (selector, root = document) => /** @type {T[]} */ ([...root.querySelectorAll(selector)]);

/** @type {Record<string, string>} */
const ENTITY = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };

/**
 * `value` as text inside HTML: & < > " ' become entities, so it is safe between tags and inside an
 * attribute quoted either way. null and undefined are the empty string.
 * @param {unknown} value
 * @returns {string}
 */
export const esc = value => String(value ?? "").replace(/[&<>"']/g, c => ENTITY[c]);

/**
 * Listen for `type` on `target`. Returns the function that stops listening.
 * @param {string} type
 * @param {(e: any) => void} fn
 * @param {EventTarget} [target]
 * @returns {() => void}
 */
export function on(type, fn, target = document) {
  target.addEventListener(type, fn);
  return () => target.removeEventListener(type, fn);
}

// The icon's picture: hills under a low sun. Shown where there are no photos to show.
export const MARK = '<svg class="mark" viewBox="0 0 150 100" aria-hidden="true"><circle class="s" cx="102" cy="26" r="15"/><path fill="currentColor" d="M0 100 49.500 38 84 74 103.500 56 150 100Z"/></svg>';
