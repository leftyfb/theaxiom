"""Conservative, bounded offline language inference for simple PDFs.

No new parser dependency: only classic xref objects and plain/Flate page
streams are interpreted. Unsupported constructs return unknown, never block
acquisition. No OCR, subprocesses, network or executable PDF content.
"""
import re
import zlib

from . import pdf_source

MAX_STREAM = 256 * 1024
MAX_SAMPLE = 32 * 1024
LANGUAGES = {
    "en": "the and of to in is that for with this from are you your can should not have be as on or",
    "es": "el la los las de del que en por para con una uno es son se al como su sus esta este",
    "fr": "le la les des du de et en que pour dans avec une un est sont ce cette vous pas sur au",
    "de": "der die das den dem des und ist sind mit für von zu im ein eine nicht auf auch sich als",
    "pt": "o os a as de do da dos das que em para com uma um são não seu sua pelo pela esse esta",
    "it": "il lo la gli le di del della che e in per con una un sono non questo questa nel delle",
}
WORDS = {code: set(words.split()) for code, words in LANGUAGES.items()}


def _objects(data):
    footer = re.search(rb"(?:^|[\r\n])startxref\s+([0-9]{1,10})\s+%%EOF[\x00\t\n\f\r ]*\Z", data[-4096:])
    if not footer:
        raise ValueError
    offset = int(footer[1])
    if not 9 <= offset < len(data) or len(data)-offset > 256*1024:
        raise ValueError
    table, sep, trailer = data[offset:].partition(b"trailer")
    match = re.match(rb"\s*<<([^<>]{1,4096})>>", trailer)
    if not sep or not table.startswith((b"xref\n", b"xref\r")) or not match or re.search(rb"/(?:Prev|XRefStm|Encrypt)\b", match[1]):
        raise ValueError
    root = re.findall(rb"/Root\s+(\d{1,10})\s+(\d{1,5})\s+R\b", match[1])
    if len(root) != 1:
        raise ValueError
    lines = table.splitlines()
    entries = {}
    i = 1
    while i < len(lines):
        header = re.fullmatch(rb"\s*(\d{1,10})\s+(\d{1,10})\s*", lines[i])
        if not header:
            raise ValueError
        first, count = map(int, header.groups())
        i += 1
        if count > len(lines)-i or len(entries)+count > 8192:
            raise ValueError
        for n in range(count):
            entry = re.fullmatch(rb"(\d{10})\s+(\d{5})\s+([nf])\s*", lines[i+n])
            if not entry:
                raise ValueError
            if entry[3] == b"n":
                key = (first+n, int(entry[2]))
                pos = int(entry[1])
                if key in entries or not 9 <= pos < offset:
                    raise ValueError
                entries[key] = pos
        i += count
    def obj(key):
        pos = entries[key]
        head = re.match(rb"(\d+)\s+(\d+)\s+obj\b", data[pos:pos+64])
        if not head or tuple(map(int, head.groups())) != key:
            raise ValueError
        return data[pos+head.end():min(offset,pos+MAX_STREAM+8192)]
    return tuple(map(int, root[0])), entries, obj


def _stream_text(body):
    match = re.match(rb"\s*<<([^<>]{1,4096})>>\s*stream\r?\n", body)
    if not match:
        return ""
    dictionary = match[1]
    lengths = re.findall(rb"/Length\s+(\d{1,8})\b(?!\s+\d+\s+R)", dictionary)
    if len(lengths) != 1:
        return ""
    length = int(lengths[0])
    if not 0 < length <= MAX_STREAM:
        return ""
    raw = body[match.end():match.end()+length]
    if len(raw) != length or not re.match(rb"\s*endstream\b", body[match.end()+length:]):
        return ""
    if re.search(rb"/Filter\b", dictionary):
        if not re.search(rb"/Filter\s*/FlateDecode\b", dictionary) or b"/DecodeParms" in dictionary:
            return ""
        decoder = zlib.decompressobj()
        raw = decoder.decompress(raw, MAX_STREAM+1)
        if len(raw) > MAX_STREAM or not decoder.eof or decoder.unconsumed_tail:
            return ""
    # Only simple, unescaped literal text operands, inside a text object.
    result = []
    for block in re.finditer(rb"\bBT\b(.*?)\bET\b", raw, re.S):
        for item in re.finditer(rb"\(([^()\\\r\n]{1,2048})\)\s*Tj\b", block[1]):
            result.append(item[1].decode("latin-1"))
    return " ".join(result)


def _classify(text):
    words = re.findall(r"[^\W\d_]+", text.casefold(), re.UNICODE)
    if len(words) < 35:
        return None
    scores = sorted(((sum(word in terms for word in words), code) for code, terms in WORDS.items()), reverse=True)
    count, code = scores[0]
    runner = scores[1][0]
    if count < max(12, len(words)*0.18) or count < 2*max(1, runner) or count-runner < 8:
        return None
    if len(set(words) & WORDS[code]) < 6:
        return None
    return code


def detect(data):
    """Return a language code or None; favor precision over coverage."""
    try:
        if not isinstance(data, bytes) or not 0 < len(data) <= pdf_source.MAX_BYTES:
            return None
        root, entries, obj = _objects(data)
        catalog = re.match(rb"\s*<<([^<>]{1,4096})>>", obj(root))
        if not catalog or not re.search(rb"/Type\s*/Catalog\b", catalog[1]):
            return None
        # Only a simple unambiguous Catalog /Lang literal is recognized.
        lang = re.findall(rb"/Lang\s*\(([A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8}){0,3})\)", catalog[1])
        declared = lang[0].decode("ascii").lower() if len(lang) == 1 else None
        samples = []
        for key in list(entries)[:8192]:
            body = obj(key)
            page = re.match(rb"\s*<<([^<>]{1,4096})>>", body)
            if not page or not re.search(rb"/Type\s*/Page\b", page[1]):
                continue
            refs = re.findall(rb"/Contents\s+(\d{1,10})\s+(\d{1,5})\s+R\b", page[1])
            if len(refs) == 1:
                sample = _stream_text(obj(tuple(map(int, refs[0]))))
                if sample:
                    samples.append(sample[:MAX_SAMPLE])
            if len(samples) >= 4 or sum(map(len, samples)) >= MAX_SAMPLE:
                break
        guesses = {_classify(sample) for sample in samples} - {None}
        if len(guesses) > 1:
            return None
        guess = _classify(" ".join(samples)[:MAX_SAMPLE])
        if declared:
            if guess and declared.split("-")[0] != guess:
                return None
            return declared
        return guess
    except (ValueError, KeyError, OverflowError, zlib.error):
        return None


def installed_language(artifact):
    try:
        with pdf_source.installed(artifact, verify=True) as stream:
            return detect(stream.read(pdf_source.MAX_BYTES+1))
    except (OSError, ValueError):
        return None
