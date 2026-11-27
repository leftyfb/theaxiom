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
// Estimate a dot unit from observed mark and gap durations. Morse marks are
// 1 or 3 units; inter-element/letter/word gaps are 1, 3 or 7 units.
// Ignore pauses longer than the maximum ordinary word gap.
function estimateUnit(marks, gaps) {
 const usable = marks.filter(d => d >= 25 && d <= 850);
 if (!usable.length) return null;
 let best = null, score = Infinity;
 const distance = (ratio, choices) => Math.min(...choices.map(n => Math.abs(ratio - n) / n));
 for (let unit = 40; unit <= 240; unit++) {
  const markLoss = usable.reduce((sum, d) => sum + Math.min(2, distance(d / unit, [1, 3])), 0) / usable.length;
  const shortGaps = gaps.filter(d => d >= 20 && d <= 1800);
  const gapLoss = shortGaps.length ? shortGaps.reduce((sum, d) =>
   sum + Math.min(1, distance(d / unit, [1, 3, 7])), 0) / shortGaps.length : 0;
  // Prefer the conventional 15 WPM only as a weak tie breaker.
  const loss = markLoss + .6 * gapLoss + .005 * Math.abs(Math.log(unit / 80));
  if (loss < score) { best = unit; score = loss; }
 }
 return best;
}
class AutoTimingDecoder {
 constructor() {
  this.marks = []; this.gaps = []; this.events = []; this.lastTone = false;
  this.edge = null; this.decoder = null; this.unit = null; this.locked = false;
 }
 sample(tone, now) {
  if (!this.locked && tone !== this.lastTone) {
   if (this.edge !== null) {
    const duration = now - this.edge;
    if (this.lastTone) { if (duration >= 25 && duration <= 850) this.marks.push(duration); }
    else if (duration >= 20 && duration <= 1800) this.gaps.push(duration);
   }
   this.events.push([tone, now]); this.edge = now; this.lastTone = tone;
   if (!tone && this.marks.length >= 4) this.calibrate();
   if (this.marks.length >= 12 || this.events.length >= 100) {
    if (!this.decoder && this.marks.length) this.calibrate();
    if (this.decoder) { this.locked = true; this.events = []; }
    else { this.events = []; this.marks = []; this.gaps = []; }
   }
  }
  return this.decoder ? this.decoder.sample(tone, now) : "";
 }
 calibrate() {
  const unit = estimateUnit(this.marks, this.gaps);
  if (unit === null) return;
  this.unit = unit; this.decoder = new TimingDecoder(1200 / unit);
  for (const [tone, time] of this.events) this.decoder.sample(tone, time);
 }
 get wpm() { return this.unit ? 1200 / this.unit : null; }
 get code() { return this.decoder?.code || ""; }
 finish(now) {
  if (!this.locked && this.lastTone) this.sample(false, now);
  if (!this.decoder && this.marks.length) this.calibrate();
  return this.decoder ? this.decoder.finish(now) : "";
 }
}
// A dominant narrow-band peak in the CW band, not just overall volume.
function detectPitch(bins, sampleRate, fftSize, threshold) {
 const scale = sampleRate / fftSize;
 const first = Math.max(4, Math.ceil(350 / scale)), last = Math.min(bins.length - 5, Math.floor(1500 / scale));
 let best = null;
 for (let i = first; i <= last; i++) {
  const level = bins[i];
  if (level < threshold || level < bins[i - 1] || level < bins[i + 1]) continue;
  const floor = Math.max(bins[i - 4], bins[i + 4]);
  if (level - floor < 10) continue;
  if (!best || level > best.level) best = {frequency: i * scale, level};
 }
 return best;
}
class PitchTracker {
 constructor(frequency = null) { this.manual = frequency; this.frequency = frequency; this.lastTone = null; }
 sample(bins, sampleRate, fftSize, threshold, now) {
  if (this.frequency !== null && tonePresent(bins, sampleRate, fftSize, this.frequency, threshold)) {
   this.lastTone = now; return true;
  }
  if (this.manual !== null) return false;
  if (this.lastTone !== null && now - this.lastTone < 1500) return false;
  const peak = detectPitch(bins, sampleRate, fftSize, threshold);
  this.frequency = peak?.frequency ?? null;
  if (peak) { this.lastTone = now; return true; }
  return false;
 }
}
function signalLevel(samples) {
 let energy = 0;
 for (const value of samples) energy += value * value;
 return Math.max(-100, 20 * Math.log10(Math.sqrt(energy / samples.length)));
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
 const auto = $("mic-auto"), meter = $("mic-level"), metrics = $("mic-metrics"), symbols = $("mic-symbols");
 function updateFields() { fields[0].disabled = fields[1].disabled = !!session || auto.checked; fields[2].disabled = !!session; auto.disabled = !!session; }
 let session = null;
 auto.onchange = updateFields;
 updateFields();
 function reset(s, message) {
  if (!s || session !== s) return;
  session = null;
  if (s.timer !== undefined) clearInterval(s.timer);
  if (s.stream) s.stream.getTracks().forEach(track => track.stop());
  if (s.context) s.context.close().catch(() => {});
  start.disabled = false; stop.disabled = true; updateFields(); meter.value = 0;
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
  const automatic = auto.checked;
  if (!fields[2].checkValidity() || !Number.isFinite(threshold) || (!automatic && (fields.slice(0, 2).some(field => !field.checkValidity()) || ![wpm, frequency].every(Number.isFinite)))) {
   status.textContent = "Enter valid speed, pitch and sensitivity."; return;
  }
  const s = {decoder: automatic ? new AutoTimingDecoder() : new TimingDecoder(wpm), tracker: new PitchTracker(automatic ? null : frequency), markCount: 0, tone: false};
  session = s; start.disabled = true; stop.disabled = false; updateFields();
  meter.value = 0; metrics.textContent = "Waiting for audio…"; symbols.textContent = "—";
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
   s.source = s.context.createMediaStreamSource(stream);
   s.source.connect(analyser);
   const bins = new Float32Array(analyser.frequencyBinCount), waveform = new Float32Array(analyser.fftSize);
   status.textContent = "Listening locally. Play Morse through speakers; watch the input level and tone indicator.";
   stream.getTracks().forEach(track => track.addEventListener("ended", () => reset(s, "Microphone disconnected.")));
   s.timer = setInterval(() => {
    if (session !== s) return;
    analyser.getFloatFrequencyData(bins);
    analyser.getFloatTimeDomainData(waveform);
    const now = performance.now(), db = signalLevel(waveform);
    const tone = s.tracker.sample(bins, s.context.sampleRate, analyser.fftSize, threshold, now);
    if (tone && !s.tone) s.markCount++;
    s.tone = tone;
    transcript.value = s.decoder.sample(tone, now);
    meter.value = Math.max(0, db + 100);
    const pitch = s.tracker.frequency === null ? "Searching" : Math.round(s.tracker.frequency) + " Hz";
    const speed = automatic ? (s.decoder.wpm ? s.decoder.wpm.toFixed(1) + " WPM (estimated)" : "Learning from tones") : wpm + " WPM";
    metrics.textContent = "Input " + Math.round(db) + " dBFS · Pitch: " + pitch + " · Speed: " + speed + " · " + (tone ? "TONE" : "No tone") + " · Marks: " + s.markCount;
    symbols.textContent = s.decoder.code || "—";
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
if (typeof module !== "undefined" && module.exports) module.exports = {MORSE, encode, decode, TimingDecoder, tonePresent, estimateUnit, AutoTimingDecoder, detectPitch, PitchTracker, signalLevel};
if (typeof document !== "undefined") init();
