"""Extract a bounded page range in a disposable subprocess."""

import io
import json
import sys


def extract(data, start, end, offset):
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    count = len(reader.pages)
    if not 1 <= start <= count or end < start:
        return {"error": "INVALID_RANGE: Page range is outside this PDF."}
    rows = []
    budget = 16000
    labels = reader.page_labels
    for number in range(start, min(end, count, start + 9) + 1):
        text = reader.pages[number - 1].extract_text() or ""
        if offset > len(text):
            return {"error": "INVALID_RANGE: Offset is outside the selected PDF page."}
        piece = text[offset : offset + budget]
        rows.append(
            {
                "page": number,
                "label": labels[number - 1],
                "text": piece,
                "has_text": bool(text.strip()),
            }
        )
        budget -= len(piece)
        if offset + len(piece) < len(text):
            return {
                "pages": rows,
                "total_pages": count,
                "next_page": number,
                "next_offset": offset + len(piece),
            }
        offset = 0
        if not budget:
            break
    last = rows[-1]["page"]
    return {
        "pages": rows,
        "total_pages": count,
        "next_page": last + 1 if last < count else None,
        "next_offset": 0,
    }


if __name__ == "__main__":
    import resource

    resource.setrlimit(resource.RLIMIT_AS, (512 * 1024**2,) * 2)
    resource.setrlimit(resource.RLIMIT_CPU, (12, 12))
    result = extract(sys.stdin.buffer.read(), *map(int, sys.argv[1:]))
    print(json.dumps(result))
