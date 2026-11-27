/* Original offline Morse utility; Apache-2.0. */
"use strict";
const MORSE = Object.freeze({
 A:".-",B:"-...",C:"-.-.",D:"-..",E:".",F:"..-.",G:"--.",H:"....",I:"..",
 J:".---",K:"-.-",L:".-..",M:"--",N:"-.",O:"---",P:".--.",Q:"--.-",
 R:".-.",S:"...",T:"-",U:"..-",V:"...-",W:".--",X:"-..-",Y:"-.--",Z:"--..",
 0:"-----",1:".----",2:"..---",3:"...--",4:"....-",5:".....",6:"-....",
 7:"--...",8:"---..",9:"----.",".":".-.-.-",",":"--..--","?":"..--..",
 "'":".----.","!":"-.-.--","/":"-..-.","(":"-.--.",")":"-.--.-",
 "&":".-...",":":"---...",";":"-.-.-.","=":"-...-","+":".-.-.","-":"-....-",
 "_":"..--.-",'"':".-..-.","$":"...-..-","@":".--.-."
});
const REVERSE = Object.assign(Object.fromEntries(Object.entries(MORSE).map(([letter, code]) => [code, letter])), {"...---...": "SOS"});
function encode(text) {
 const unknown = new Set();
 const output = text.trim().split(/\s+/).filter(Boolean).map(word =>
  Array.from(word, letter => {
   const upper = letter.toUpperCase();
   if (Object.hasOwn(MORSE, upper)) return MORSE[upper];
   unknown.add(letter); return "[?]";
  }).join(" ")).join(" / ");
 return {output, unknown: [...unknown]};
}
function decode(text) {
 const unknown = new Set();
 const output = text.trim().replace(/[·•]/g, ".").replace(/[–—−]/g, "-")
  .split(/\s*\/\s*|\s{3,}/).map(word => word.trim().split(/\s+/).filter(Boolean).map(code => {
   if (Object.hasOwn(REVERSE, code)) return REVERSE[code];
   unknown.add(code); return "�";
  }).join("")).filter(Boolean).join(" ");
 return {output, unknown: [...unknown]};
}
// Fixed-speed decoder. Feed timestamped tone/no-tone samples in milliseconds.
// A midpoint between nominal Morse durations separates dots/dashes and gaps.
class TimingDecoder {
 constructor(wpm) {
  if (!Number.isFinite(wpm) || wpm < 5 || wpm > 30) throw new RangeError("Invalid speed");
  this.unit = 1200 / wpm;
  this.tone = false; this.since = null; this.code = ""; this.output = "";
  this.letterDone = true; this.wordDone = true; this.last = null;
 }
 gap(now) {
  const elapsed = now - this.since;
  if (!this.letterDone && elapsed >= 2 * this.unit) {
   this.output += REVERSE[this.code] || "�"; this.code = ""; this.letterDone = true;
  }
  if (!this.wordDone && elapsed >= 5 * this.unit) {
   if (this.output && !this.output.endsWith(" ")) this.output += " ";
   this.wordDone = true;
  }
 }
 sample(tone, now) {
  if (!Number.isFinite(now) || (this.last !== null && now < this.last)) return this.output;
  this.last = now;
  if (this.since === null) this.since = now;
  if (tone !== this.tone) {
   if (this.tone) {
    const duration = now - this.since;
    if (duration >= .45 * this.unit) {
     this.code += duration < 2 * this.unit ? "." : "-";
     this.letterDone = false; this.wordDone = false;
    }
   } else this.gap(now);
   this.tone = tone; this.since = now;
  }
  if (!tone) this.gap(now);
  return this.output;
 }
 finish(now) {
  if (this.tone) this.sample(false, now);
  if (this.code) { this.output += REVERSE[this.code] || "�"; this.code = ""; }
  return this.output.trimEnd();
 }
}
// Require energy near the selected pitch above both absolute and nearby noise floors.
function tonePresent(bins, sampleRate, fftSize, frequency, threshold) {
 const center = Math.round(frequency * fftSize / sampleRate);
 if (center < 4 || center + 4 >= bins.length) return false;
 const signal = Math.max(...bins.slice(center - 1, center + 2));
 const noise = Math.max(bins[center - 4], bins[center + 4]);
 return signal >= threshold && signal - noise >= 8;
}
function init() {
 const $ = id => document.getElementById(id);
 const input = $("morse-input"), output = $("morse-output"), notice = $("morse-notice");
 function convert(fn) {
  const result = fn(input.value);
  output.value = result.output;
  notice.textContent = result.unknown.length ? "Unrecognized input: " + result.unknown.join(", ") : "";
 }
 $("morse-encode").onclick = () => convert(encode);
 $("morse-decode").onclick = () => convert(decode);
 $("morse-use-output").onclick = () => { input.value = output.value; input.focus(); };
 const reference = $("morse-reference");
 for (const [letter, code] of Object.entries(MORSE).sort(([a], [b]) => {
  const rank = s => /^[A-Z]$/.test(s) ? 0 : /^\d$/.test(s) ? 1 : 2;
  return rank(a) - rank(b) || (a < b ? -1 : a > b ? 1 : 0);
 })) {
  const item = document.createElement("div"), symbol = document.createElement("strong");
  const value = document.createElement("code"); symbol.textContent = letter; value.textContent = code;
  item.append(symbol, value); reference.append(item);
 }
 const start = $("mic-start"), stop = $("mic-stop"), status = $("mic-status"), transcript = $("mic-output");
 const fields = [$("mic-speed"), $("mic-frequency"), $("mic-threshold")];
 let session = null;
 function reset(s, message) {
  if (!s || session !== s) return;
  session = null;
  if (s.timer !== undefined) clearInterval(s.timer);
  if (s.stream) s.stream.getTracks().forEach(track => track.stop());
  if (s.context) s.context.close().catch(() => {});
  start.disabled = false; stop.disabled = true; fields.forEach(field => { field.disabled = false; });
  status.textContent = message;
 }
 const Audio = window.AudioContext || window.webkitAudioContext;
 if (!window.isSecureContext || !navigator.mediaDevices?.getUserMedia || !Audio) {
  start.disabled = true;
  status.textContent = "Microphone unavailable. Use trusted local HTTPS and a compatible browser, or use manual decoding above.";
  return;
 }
 start.onclick = async () => {
  if (session) return;
  const [wpm, frequency, threshold] = fields.map(field => field.valueAsNumber);
  if (fields.some(field => !field.checkValidity()) || ![wpm, frequency, threshold].every(Number.isFinite)) {
   status.textContent = "Enter valid speed, pitch and sensitivity."; return;
  }
  const s = {decoder: new TimingDecoder(wpm)};
  session = s; start.disabled = true; stop.disabled = false;
  fields.forEach(field => { field.disabled = true; });
  transcript.value = ""; status.textContent = "Requesting microphone permission…";
  try {
   s.context = new Audio();
   const resume = s.context.resume();
   // Attach a handler immediately in case the browser rejects before permission resolves.
   resume.catch(() => {});
   const stream = await navigator.mediaDevices.getUserMedia({audio: {
    echoCancellation: false, noiseSuppression: false, autoGainControl: false
   }});
   if (session !== s) { stream.getTracks().forEach(track => track.stop()); return; }
   s.stream = stream; await resume;
   if (session !== s) return;
   const analyser = s.context.createAnalyser();
   analyser.fftSize = 2048; analyser.smoothingTimeConstant = 0;
   s.context.createMediaStreamSource(stream).connect(analyser);
   const bins = new Float32Array(analyser.frequencyBinCount);
   status.textContent = "Listening locally. Match the sender’s pitch and speed; use a clean tone.";
   stream.getTracks().forEach(track => track.addEventListener("ended", () => reset(s, "Microphone disconnected.")));
   s.timer = setInterval(() => {
    if (session !== s) return;
    analyser.getFloatFrequencyData(bins);
    transcript.value = s.decoder.sample(tonePresent(bins, s.context.sampleRate, analyser.fftSize, frequency, threshold), performance.now());
   }, 10);
  } catch (error) {
   reset(s, error.name === "NotAllowedError" ? "Microphone permission denied. Allow it in browser settings or use manual decoding." :
    "Microphone could not start. Check browser permissions and audio input; manual decoding remains available.");
  }
 };
 stop.onclick = () => {
  const s = session;
  if (s) { if (s.timer !== undefined) transcript.value = s.decoder.finish(performance.now()); reset(s, "Microphone stopped."); }
 };
 window.addEventListener("pagehide", () => reset(session, "Microphone stopped."));
 document.addEventListener("visibilitychange", () => {
  if (document.hidden) reset(session, "Microphone stopped when page was hidden.");
 });
}
if (typeof module !== "undefined" && module.exports) module.exports = {MORSE, encode, decode, TimingDecoder, tonePresent};
if (typeof document !== "undefined") init();
