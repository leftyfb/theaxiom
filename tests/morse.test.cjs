const test = require("node:test");
const assert = require("node:assert/strict");
const {MORSE, encode, decode, TimingDecoder, tonePresent, AutoTimingDecoder, PitchTracker, detectPitch, signalLevel} = require("../templates/morse.js");

test("encode and decode International Morse text and punctuation", () => {
 assert.deepEqual(encode("SOS  Test 123!"), {output: "... --- ... / - . ... - / .---- ..--- ...-- -.-.--", unknown: []});
 assert.deepEqual(decode("... --- ...   - . ... - / .---- ..--- ...-- -.-.--"), {output: "SOS TEST 123!", unknown: []});
 assert.equal(decode("...---...").output, "SOS");
 assert.equal(decode("·− −···").output, "AB");
 for (const [letter, code] of Object.entries(MORSE)) {
  assert.equal(encode(letter).output, code);
  assert.equal(decode(code).output, letter);
 }
});
test("empty and unsupported characters remain explicit", () => {
 assert.deepEqual(encode("  "), {output: "", unknown: []});
 assert.deepEqual(decode(" / "), {output: "", unknown: []});
 assert.deepEqual(encode("Aß🚀"), {output: ".- [?] [?]", unknown: ["ß", "🚀"]});
 assert.deepEqual(decode(".- ........"), {output: "A�", unknown: ["........"]});
});
function play(text, wpm) {
 const decoder = new TimingDecoder(wpm), unit = 1200 / wpm;
 let t = 0;
 decoder.sample(false, t);
 const words = text.split(" ");
 words.forEach((word, wi) => {
  [...word].forEach((letter, li) => {
   const symbols = [...MORSE[letter]];
   symbols.forEach((symbol, si) => {
    decoder.sample(true, t);
    t += unit * (symbol === "." ? 1 : 3);
    decoder.sample(false, t);
    if (si < symbols.length - 1) t += unit;
   });
   if (li < word.length - 1) t += unit * 3;
  });
  if (wi < words.length - 1) t += unit * 7;
 });
 return decoder.finish(t);
}
test("tone timing decodes letters and words over the supported speed range", () => {
 for (const speed of [5, 15, 30]) assert.equal(play("SOS TEST 123", speed), "SOS TEST 123");
 assert.throws(() => new TimingDecoder(0), RangeError);
 const d = new TimingDecoder(15);
 d.sample(true, 0); d.sample(false, 10);
 assert.equal(d.finish(10), "");
});
test("tone detector distinguishes selected pitch, silence and broadband noise", () => {
 const bins = new Float32Array(1024).fill(-100), center = Math.round(700 * 2048 / 48000);
 assert.equal(tonePresent(bins, 48000, 2048, 700, -55), false);
 bins[center] = -35;
 assert.equal(tonePresent(bins, 48000, 2048, 700, -55), true);
 assert.equal(tonePresent(bins, 48000, 2048, 1000, -55), false);
 bins.fill(-30);
 assert.equal(tonePresent(bins, 48000, 2048, 700, -55), false);
});

test("automatic speed learns and replays initial tones across common speeds", () => {
 for (const speed of [5, 8, 12, 15, 20, 25, 30]) {
  const decoder = new AutoTimingDecoder(), unit = 1200 / speed;
  let t = 0;
  const words = "SOS TEST 123".split(" ");
  decoder.sample(false, t);
  words.forEach((word, wi) => [...word].forEach((letter, li) => {
   [...MORSE[letter]].forEach((symbol, si) => {
    decoder.sample(true, t);
    t += unit * (symbol === "." ? 1 : 3);
    decoder.sample(false, t);
    if (si < MORSE[letter].length - 1) t += unit;
   });
   if (li < word.length - 1) t += 3 * unit;
   else if (wi < words.length - 1) t += 7 * unit;
  }));
  assert.equal(decoder.finish(t), "SOS TEST 123");
  assert.ok(Math.abs(decoder.wpm - speed) < .6);
 }
});
test("automatic pitch finds different clean tones and ignores broadband noise", () => {
 const bins = new Float32Array(1024).fill(-100);
 for (const pitch of [450, 600, 800, 1000, 1350]) {
  bins.fill(-100);
  const center = Math.round(pitch * 2048 / 48000);
  bins[center] = -48;
  assert.ok(Math.abs(detectPitch(bins, 48000, 2048, -75).frequency - pitch) < 24);
  const tracker = new PitchTracker();
  assert.equal(tracker.sample(bins, 48000, 2048, -75, 0), true);
  bins.fill(-100);
  assert.equal(tracker.sample(bins, 48000, 2048, -75, 80), false);
  assert.ok(Math.abs(tracker.frequency - pitch) < 24);
 }
 bins.fill(-40);
 assert.equal(detectPitch(bins, 48000, 2048, -75), null);
 assert.equal(signalLevel(new Float32Array(64)), -100);
 assert.ok(Math.abs(signalLevel(new Float32Array(64).fill(.1)) + 20) < .001);
});
