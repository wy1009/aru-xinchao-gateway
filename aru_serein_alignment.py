"""Locate id-less Aru messages within one already-resolved conversation."""
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
import re

_SENT = re.compile(r"\n\n<sent_at (\d{4}-\d{2}-\d{2} \d{2}:\d{2})>\s*\Z")
_BEIJING = timezone(timedelta(hours=8))


def split_original(text):
    match = _SENT.search(text)
    return (text[:match.start()], match[1]) if match else (text, None)


def minor_edit(old, new):
    """One contiguous correction of at most two characters, inside otherwise matching text."""
    if old == new or min(len(old),len(new)) < 3 or abs(len(old)-len(new)) > 2:
        return False
    prefix = 0
    while prefix < min(len(old),len(new)) and old[prefix] == new[prefix]:
        prefix += 1
    old_end, new_end = len(old), len(new)
    while min(old_end,new_end) > prefix and old[old_end-1] == new[new_end-1]:
        old_end -= 1
        new_end -= 1
    return max(old_end-prefix,new_end-prefix) <= 2 and prefix+len(old)-old_end >= 3


def chronology(conn, session):
    return conn.execute("""SELECT * FROM aru_original WHERE session=? AND source IN ('aru','aru-live')
        ORDER BY (source='aru-live'),seq,stamp,(role='assistant'),mid""", (session,)).fetchall()


def align(messages, rows):
    """Return source positions, plus whether the current question needs more evidence.

    Exact anchors occur in both transcripts. Only pair a changed historical
    slot between matching neighbors; a trailing user edit also needs its clock.
    A clipped window is never evidence that an absent message was deleted.
    """
    stored = [split_original(r["text"]) for r in rows]
    clocks = [clock or (datetime.fromtimestamp(r["stamp"] / 1000, _BEIJING).strftime("%Y-%m-%d %H:%M")
                        if r["source"] == "aru" else None)
              for r, (_, clock) in zip(rows, stored)]
    texts = defaultdict(list)
    ids = {}
    for i, row in enumerate(rows):
        texts[row["role"], stored[i][0]].append(i)
        ids[row["role"], row["mid"]] = i
    parts = [split_original(m["text"]) for m in messages]
    last = len(messages) - 1
    anchors = {}
    def fits(i, j):
        msg, row = messages[i], rows[j]
        return (msg["role"] == row["role"] and
                (not msg.get("mid") or msg["mid"] == row["mid"]) and
                (not parts[i][1] or not clocks[j] or parts[i][1] == clocks[j]))
    for i, m in enumerate(messages):
        if m.get("mid"):
            if (m["role"], m["mid"]) in ids:
                anchors[i] = ids[m["role"], m["mid"]]
        elif i < last:
            hits = [j for j in texts[m["role"], parts[i][0]] if fits(i, j)]
            if len(hits) == 1:
                anchors[i] = hits[0]

    # A quotation may occur twice in the visible prefix, or the client could
    # have selected another branch. Never bridge a reversal or reused anchor.
    counts = Counter(anchors.values())
    anchors = {i:j for i,j in anchors.items() if counts[j] == 1}
    previous = -1
    for i,j in list(anchors.items()):
        if j <= previous:
            # Use only the unambiguous tail of a rearranged or branched window.
            anchors = {n:p for n,p in anchors.items() if n >= i}
        previous = j
    # A known terminal question can bound a correction to the preceding reply.
    # Verify its location relative to an independent anchor; repeated words
    # elsewhere in this conversation cannot identify today's question.
    if anchors and last not in anchors and not messages[last].get("mid"):
        i = next(reversed(anchors))
        j = anchors[i]+last-i
        if (j < len(rows) and fits(last,j) and parts[last][0] == stored[j][0]
                and all(messages[n]["role"] == rows[j-last+n]["role"] for n in range(i+1,last))):
            anchors[last] = j
    matched = dict(anchors)
    for (i,j),(k,l) in zip(anchors.items(), list(anchors.items())[1:]):
        if k-i != l-j: continue
        # Recover repeated identical text using its position, even when the
        # same words elsewhere could not act as a unique anchor.
        for n in range(i+1,k):
            p = j+n-i
            if fits(n,p) and parts[n][0] == stored[p][0]:
                matched[n] = p
    # Only a single, same-role hole bounded by two verified messages can
    # replace historical text without a client ID.
    for i in range(1,last):
        left, right = matched.get(i-1), matched.get(i+1)
        if i not in matched and left is not None and right == left+2 and fits(i,left+1):
            matched[i] = left+1

    # Follow the last exact anchor through the remaining unchanged suffix.
    # A regenerated historical assistant may also be followed by a brand new
    # question, in which case the already identified parent anchors the reply.
    pending = False
    if anchors:
        left = next(reversed(anchors))
        for i in range(left+1,last+1):
            j = matched[i-1]+1
            if j >= len(rows): break
            same = parts[i][0] == stored[j][0]
            tail_question = (i == last and not messages[i].get("mid") and
                             messages[i]["role"] == rows[j]["role"] == "user")
            preceding_pair = i >= 2 and matched.get(i-2) == j-2
            typo = tail_question and preceding_pair and minor_edit(parts[i][0], stored[j][0])
            if not fits(i,j):
                if not (tail_question and preceding_pair and (same or typo)):
                    pending = tail_question
                    break
            same_clock = parts[i][1] and parts[i][1] == clocks[j]
            # The slot immediately following a known question is its answer.
            paired_answer = (i == last-1 and messages[i]["role"] == "assistant"
                             and messages[i-1]["role"] == "user"
                             and parts[i-1][0] == stored[j-1][0]
                             and j == len(rows)-1)
            if not (same or same_clock or paired_answer or typo):
                pending = i == last and not messages[i].get("mid")
                break
            matched[i] = j
    if last not in matched and not messages[last].get("mid") and not anchors:
        # A headerless clipped request may present only the question.
        # Its clock and wording identify a candidate, but not its location.
        pending = bool(parts[last][1] and any(
            fits(last,j) and clocks[j] == parts[last][1]
            for j in texts[messages[last]["role"],parts[last][0]]))
    return matched, pending
