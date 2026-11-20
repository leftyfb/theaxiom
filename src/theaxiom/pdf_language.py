"""Best-effort offline PDF language inference in a resource-bounded worker."""
import io
import logging
import re
import subprocess
import sys
import threading

from . import pdf_source

MAX_SAMPLE = 32 * 1024
MAX_PAGES = 8
DETECTION_TIMEOUT = 5.0
MAX_MEMORY = 384 * 1024 * 1024
LANG = re.compile(r"[a-z]{2,3}(?:-[a-z0-9]{2,8}){0,3}\Z")
LOG = logging.getLogger(__name__)
SLOTS = threading.BoundedSemaphore(1)


def _classify(text):
    from langdetect import DetectorFactory, detect_langs
    from langdetect.lang_detect_exception import LangDetectException

    # Ignore numeric labels, isolated letters and punctuation; keep scripts
    # without spaces intact. A short or ambiguous sample is not trustworthy.
    text = re.sub(r"[\W\d_]+", " ", text, flags=re.UNICODE).strip()
    if sum(ch.isalpha() for ch in text) < 60:
        return None
    if len(set(ch.casefold() for ch in text if ch.isalpha())) < 10:
        return None
    DetectorFactory.seed = 0
    try:
        results = detect_langs(text[:MAX_SAMPLE])
    except LangDetectException:
        return None
    if not results or results[0].prob < .90:
        return None
    value = results[0].lang.lower()
    return value if LANG.fullmatch(value) else None


def _detect(data):
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data), strict=False)
    if reader.is_encrypted:
        return None
    root = reader.trailer["/Root"]
    raw = root.get("/Lang")
    declared = str(raw).strip().lower() if raw is not None else ""
    declared = declared if LANG.fullmatch(declared) and declared != "und" else None
    samples = []
    remaining = MAX_SAMPLE
    count = len(reader.pages)
    # Sample across the document, not just its front cover.
    indices = sorted(set(range(min(count, 4))) |
                     {count * i // 4 for i in range(1, 4) if count})
    for index in indices[:MAX_PAGES]:
        if not remaining:
            break
        text = (reader.pages[index].extract_text() or "")[:remaining]
        if text.strip():
            samples.append(text)
            remaining -= len(text)
    guesses = {_classify(sample) for sample in samples} - {None}
    # Check substantial blocks independently for mixed-language documents,
    # including those with multiple languages on a single page.
    combined = "\n".join(samples)
    if len(combined) >= 300:
        mid = len(combined) // 2
        guesses.update({_classify(combined[:mid]), _classify(combined[mid:])} - {None})
    if len(guesses) > 1:
        return None
    guess = _classify(combined)
    if declared:
        if any(code.split("-")[0] != declared.split("-")[0] for code in guesses | ({guess} if guess else set())):
            return None
        return declared
    if guess and (not guesses or guess in guesses):
        return guess
    return None


def detect(data):
    """Never let optional metadata prevent acquisition or overwhelm the HTTP process."""
    if not isinstance(data, bytes) or not 0 < len(data) <= pdf_source.MAX_BYTES:
        return None
    if not SLOTS.acquire(blocking=False):
        return None
    try:
        completed = subprocess.run(
            [sys.executable, "-m", "theaxiom.pdf_language"],
            input=data, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=DETECTION_TIMEOUT, check=True,
        )
        value = completed.stdout.strip().decode("ascii")
        return value if LANG.fullmatch(value) else None
    except (OSError, subprocess.SubprocessError, UnicodeError):
        LOG.warning("PDF language inference worker unavailable or exceeded its resource budget")
        return None
    finally:
        SLOTS.release()


def installed_language(artifact):
    try:
        with pdf_source.installed(artifact, verify=True) as stream:
            return detect(stream.read(pdf_source.MAX_BYTES+1))
    except (OSError, ValueError):
        return None


def _worker():
    # Set limits before loading parser/model or reading untrusted bytes.
    import resource
    resource.setrlimit(resource.RLIMIT_AS, (MAX_MEMORY, MAX_MEMORY))
    resource.setrlimit(resource.RLIMIT_CPU, (4, 4))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    data = sys.stdin.buffer.read(pdf_source.MAX_BYTES + 1)
    if not 0 < len(data) <= pdf_source.MAX_BYTES:
        return
    try:
        print(_detect(data) or "")
    except Exception:
        # A parser error is not evidence of any language.
        return


if __name__ == "__main__":
    _worker()
