"""Clipper-style word-by-word karaoke captions for narrated recaps."""
import difflib
import re


def clock(t):
    t = round(t * 100)
    return f'{t // 360000}:{t // 6000 % 60:02}:{t // 100 % 60:02}.{t % 100:02}'


def timed_words(text, duration, recognized=()):
    words = text.replace('\\', '').replace('{', '').replace('}', '').split()
    normalize = lambda s: re.sub(r'[\W]', '', s).casefold()
    anchors = {}
    match = difflib.SequenceMatcher(
        a=[normalize(w) for w in words],
        b=[normalize(w['word']) for w in recognized],
        autojunk=False)
    for block in match.get_matching_blocks():
        for offset in range(block.size):
            point = recognized[block.b + offset]
            anchors[block.a + offset] = max(0, min(duration, float(point['start'])))
    # Fill unrecognized words between measured anchors; do not replace the
    # reviewed narration with ASR guesses. No timestamps means estimated pacing.
    points = {0: 0., len(words): duration, **anchors}
    points[len(words)] = duration
    ordered = sorted(points)
    starts = [0.] * (len(words) + 1)
    for a, b in zip(ordered, ordered[1:]):
        end = max(points[a], points[b])
        for i in range(a, b + 1):
            starts[i] = points[a] + (end - points[a]) * (i - a) / (b - a)
    return [{'word': w, 'start': starts[i], 'end': starts[i + 1]} for i, w in enumerate(words)]


def _group_words(words):
    """
    Group words into 1-2 word chunks for fast karaoke display.
    Split on punctuation so a comma/period doesn't carry over to next group.
    """
    groups = []
    group = []
    for word in words:
        group.append(word)
        ends_sentence = word['word'].endswith(('.', '!', '?'))
        ends_phrase = word['word'].endswith((',', ':', ';'))
        # Flush at 2 words, or immediately on sentence end, or at phrase break
        if len(group) >= 2 or ends_sentence or ends_phrase:
            groups.append(group)
            group = []
    if group:
        groups.append(group)
    return groups


def write_captions(path, text, duration, recognized=()):
    words = timed_words(text, duration, recognized)
    groups = _group_words(words)

    # Clipper-style ASS: Noto Sans, white uppercase, thick black outline,
    # bottom-centre, matching the main Clipper render pipeline exactly.
    # PlayRes matches the portrait render canvas (1080×1920).
    header = """\
[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 0

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Noto Sans,95,&H00FFFFFF,&H000000FF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,8,4,2,20,20,500,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    events = []
    for group in groups:
        # Scale font down if a single word is very long, otherwise keep 95px
        max_word_len = max(len(w['word']) for w in group)
        size = min(95, max(60, int(900 / max(1, max_word_len))))

        # Render entire group as one subtitle line.
        # Active (currently spoken) word is highlighted in accent blue;
        # the rest are white — matching Clipper's karaoke word-emphasis style.
        group_start = group[0]['start']
        group_end = group[-1]['end']
        if group_end - group_start < 0.01:
            continue

        # One dialogue event per spoken word inside the group: the active word
        # is coloured, the others are white, so only 1-2 words appear at once.
        for active_idx, active_word in enumerate(group):
            seg_start = active_word['start']
            seg_end = active_word['end']
            if seg_end - seg_start < 0.01:
                continue
            line = ' '.join(
                (r'{\c&H0066D8FF&}' if i == active_idx else r'{\c&H00FFFFFF&}') + w['word'].upper()
                for i, w in enumerate(group)
            )
            events.append(
                f"Dialogue: 0,{clock(seg_start)},{clock(seg_end)},Default,,0,0,0,,{{\\fs{size}}}{line}\n"
            )

    path.write_text(header + ''.join(events))
