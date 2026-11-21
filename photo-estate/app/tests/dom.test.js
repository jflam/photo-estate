// dom.js and format.js without a browser: `npm test` in photo-estate/app (node --test).
import test from "node:test";
import assert from "node:assert/strict";

import { $, $$, esc, on, MARK } from "../dom.js";
import { fmt, plural, dateParts } from "../format.js";

test("esc escapes the five characters that end text or an attribute", () => {
  assert.equal(esc(`<b a="1" c='2'>&</b>`), "&lt;b a=&quot;1&quot; c=&#39;2&#39;&gt;&amp;&lt;/b&gt;");
});

test("esc: a name with an apostrophe cannot close a single-quoted attribute (issue #89)", () => {
  const name = "x' onmouseover='alert(1)";
  const html = `<button data-name='${esc(name)}'>`;
  assert.equal(html, "<button data-name='x&#39; onmouseover=&#39;alert(1)'>");
  assert.equal(html.split("'").length, 3, "the only quotes left are the attribute's own");
});

test("esc: an entity is escaped again, not passed through", () => {
  assert.equal(esc("&amp; &#39;"), "&amp;amp; &amp;#39;");
});

test("esc: null and undefined are empty; other values are their text", () => {
  assert.equal(esc(null), "");
  assert.equal(esc(undefined), "");
  assert.equal(esc(0), "0");
  assert.equal(esc(false), "false");
  assert.equal(esc(12.5), "12.5");
  assert.equal(esc("plain text, unchanged: é ’ – 日本"), "plain text, unchanged: é ’ – 日本");
});

test("$ and $$ ask the root they are given", () => {
  const a = { id: "a" }, b = { id: "b" };
  const root = { querySelector: s => (s === ".x" ? a : null), querySelectorAll: s => (s === ".x" ? new Set([a, b]) : []) };
  assert.equal($(".x", /** @type {any} */ (root)), a);
  assert.equal($(".none", /** @type {any} */ (root)), null);
  assert.deepEqual($$(".x", /** @type {any} */ (root)), [a, b]);
  assert.ok(Array.isArray($$(".none", /** @type {any} */ (root))));
});

test("on listens, and what it returns stops listening", () => {
  const target = new EventTarget();
  let heard = 0;
  const off = on("ping", () => heard++, target);
  target.dispatchEvent(new Event("ping"));
  off();
  target.dispatchEvent(new Event("ping"));
  assert.equal(heard, 1);
});

test("MARK is the icon's picture", () => {
  assert.match(MARK, /^<svg class="mark" [^>]*aria-hidden="true">.*<\/svg>$/);
});

test("fmt groups thousands; plural picks the noun", () => {
  assert.equal(fmt(7), "7");
  assert.equal(fmt("1234567"), (1234567).toLocaleString());
  assert.equal(plural(1, "photo"), "1 photo");
  assert.equal(plural(0, "photo"), "0 photos");
  assert.equal(plural(2, "person", "people"), "2 people");
  assert.equal(plural(1204, "photo"), `${(1204).toLocaleString()} photos`);
});

test("dateParts reads a timestamp as wall-clock time, whatever zone it carries", () => {
  const d = dateParts("2016-07-04T09:30:15+02:00");
  assert.ok(d);
  assert.deepEqual([d.getFullYear(), d.getMonth(), d.getDate(), d.getHours(), d.getMinutes()], [2016, 6, 4, 9, 30]);
  const day = dateParts("2009-10-07");
  assert.ok(day);
  assert.deepEqual([day.getFullYear(), day.getMonth(), day.getDate(), day.getHours()], [2009, 9, 7, 0]);
  const month = dateParts("2017-12");
  assert.ok(month);
  assert.deepEqual([month.getFullYear(), month.getMonth(), month.getDate()], [2017, 11, 1]);
});

test("dateParts: what is not a date is null", () => {
  for (const bad of ["", null, undefined, "2016", "undated", "07/04/2016"]) assert.equal(dateParts(bad), null, String(bad));
});
