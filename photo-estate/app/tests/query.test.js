// query.js: the Search tab's question and its address. `npm test` in photo-estate/app.
import test from "node:test";
import assert from "node:assert/strict";

import { KEYS, blank, copy, toParams, fromParams, filtered, isEmpty, growsFrom } from "../query.js";

test("the empty question: no address, nothing asked", () => {
  const s = blank();
  assert.equal(String(toParams(s)), "");
  assert.equal(isEmpty(s), true);
  assert.equal(filtered(s), false);
  assert.deepEqual(Object.keys(s.f), [...KEYS]);
  assert.deepEqual(fromParams(new URLSearchParams("")), s);
});

test("words alone: asked, not filtered", () => {
  const s = fromParams(new URLSearchParams("q=latte+art"));
  assert.equal(s.q, "latte art");
  assert.equal(isEmpty(s), false);
  assert.equal(filtered(s), false);
  assert.equal(String(toParams(s)), "q=latte+art");
});

test("every part of a question survives the address and back", () => {
  const s = blank();
  Object.assign(s, { q: "kids at the lake & more", plain: true, event: "e12", stack: "abc", fav: true, from: "2015-06", to: "2016-08", sort: "best", video: true, hidden: true });
  s.f.person = ["Ada O'Neil", "Ben"];
  s.f.city = ["Paris, Île-de-France, France"];
  s.f.trip = ["7"];
  const address = String(toParams(s));
  assert.deepEqual(fromParams(new URLSearchParams(address)), s);
  assert.equal(String(toParams(fromParams(new URLSearchParams(address)))), address);
});

test("the address is written in one order, facets in KEYS order", () => {
  const s = blank();
  s.f.trip = ["3"]; s.f.country = ["France", "Italy"]; s.q = "harbour"; s.fav = true; s.sort = "old"; s.from = "2014-01";
  assert.equal(String(toParams(s)), "q=harbour&country=France&country=Italy&trip=3&from=2014-01&sort=old&fav=1");
});

test("names=0 goes with words only", () => {
  const s = blank();
  s.plain = true;
  assert.equal(String(toParams(s)), "", "no words: nothing for it to apply to");
  s.q = "rose";
  assert.equal(String(toParams(s)), "q=rose&names=0");
  assert.equal(fromParams(new URLSearchParams("q=rose&names=0")).plain, true);
  assert.equal(fromParams(new URLSearchParams("q=rose&names=1")).plain, false);
});

test("more like this photo: asked, by sha", () => {
  const s = fromParams(new URLSearchParams("sha=0f3a"));
  assert.equal(s.sha, "0f3a");
  assert.equal(isEmpty(s), false);
  assert.equal(filtered(s), false);
});

test("anything but the words and the photo is a filter", () => {
  for (const address of ["person=Ada", "country=France", "event=e1", "stack=abc", "fav=1", "from=2015-06", "to=2015-06", "lens=50mm", "hidden=1"]) {
    const s = fromParams(new URLSearchParams(address));
    assert.equal(filtered(s), true, address);
    assert.equal(isEmpty(s), false, address);
  }
  assert.equal(filtered(fromParams(new URLSearchParams("sort=old"))), false, "an order filters nothing");
});

test("an address typed by hand: unknown keys are ignored, fav must be 1", () => {
  const s = fromParams(new URLSearchParams("q=sea&utm=x&fav=yes&person=&cursor=40"));
  assert.equal(s.fav, false);
  assert.deepEqual(s.f.person, [""]);
  assert.equal(String(toParams(s)), "q=sea&person=");
});

test("copy can be changed without changing the question it came from", () => {
  const s = fromParams(new URLSearchParams("q=sea&person=Ada"));
  const c = copy(s);
  assert.deepEqual(c, s);
  c.f.person.push("Ben"); c.f.city.push("Oslo"); c.q = "lake";
  assert.deepEqual(s.f.person, ["Ada"]);
  assert.deepEqual(s.f.city, []);
  assert.equal(s.q, "sea");
});

test("blank() is a new question each time", () => {
  const a = blank(), b = blank();
  a.f.album.push("Summer");
  assert.deepEqual(b.f.album, []);
});

test("videos only: kind=video in the address, and a filter", () => {
  const s = blank();
  assert.equal(s.video, false);
  s.video = true;
  assert.equal(String(toParams(s)), "kind=video");
  assert.equal(filtered(s), true);
  assert.equal(isEmpty(s), false, "every video is a question by itself");
  assert.equal(fromParams(new URLSearchParams("q=waterfall&kind=video")).video, true);
  const stills = fromParams(new URLSearchParams("kind=photo"));
  assert.deepEqual([stills.video, stills.photo], [false, true]);
  assert.equal(String(toParams(stills)), "kind=photo");
  assert.equal(filtered(stills), true);
  assert.equal(isEmpty(stills), false);
  assert.equal(fromParams(new URLSearchParams("kind=video")).photo, false);
  const c = copy(s); c.video = false;
  assert.equal(s.video, true);
});

test("growsFrom: the question as search_photos takes it, for a series; null when a series can't grow from it", () => {
  const p = (/** @type {string} */ q) => fromParams(new URLSearchParams(q));
  assert.deepEqual(growsFrom(p("q=kitten")), { query: "kitten" });
  assert.deepEqual(growsFrom(p("q=beach&person=Ben&person=Cleo&city=Pokolbin&from=2017-01&to=2018-12&fav=1&kind=photo")),
    { query: "beach", people: ["Ben", "Cleo"], place: "Pokolbin", date_from: "2017-01", date_to: "2018-12", favorite: true, kind: "photo" });
  assert.deepEqual(growsFrom(p("album=Summer&camera=X100&trip=12&event=99&kind=video")), { album: "Summer", camera: "X100", trip: 12, event: 99, kind: "video" });
  assert.deepEqual(growsFrom(p("country=Australia")), { place: "Australia" });
  for (const q of ["", "sha=ab", "stack=ab", "q=x&hidden=1", "q=x&sort=name", "lens=50mm", "city=A&city=B", "city=A&country=B", "album=A&album=B"]) {
    assert.equal(growsFrom(p(q)), null, q);
  }
});
