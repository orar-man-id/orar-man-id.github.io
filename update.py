#!/usr/bin/env python3
"""Rebuild the timetable site from the university's published Google Doc.

Usage:
  python update.py                      # fetch the published document
  python update.py --from-html doc.html # use a saved copy of the page
  python update.py --from-text doc.txt  # use text copied from the page
Options:
  --summary FILE   write a Markdown summary (changes, warnings or the error)

Exit codes: 0 = success (files may or may not have changed), 2 = the document
could not be read safely; nothing was written.
"""
import argparse
import datetime as dt
import html.parser
import json
import os
import re
import sys
import unicodedata
import urllib.request
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.abspath(__file__))   # everything lives at the top level of the repository
DOC_URL = ("https://docs.google.com/document/d/e/"
           "2PACX-1vSVuDKEvYgpVMPanAge307vo-qKQK-xHiBVdgBaBJlohbev9zzfDwfFhr6cFOWR3g/pub")
TZ = ZoneInfo("Europe/Bucharest")
DURATION_MIN = 90          # the document lists start times only
GROUPS = ["168", "169", "170", "171", "172"]

# code: (Romanian name, English name, short label, type RO, type EN)
SUBJECTS = {
    "DA":   ("Dreptul afacerilor", "Business Law", "Business Law", "Obligatoriu", "Mandatory"),
    "FIL":  ("Filosofie", "Philosophy", "Philosophy", "Obligatoriu", "Mandatory"),
    "GCSM": ("Guvernanța corporativă, socială și de mediu",
             "Corporate, Social and Environmental Governance (ESG)", "ESG Governance", "Obligatoriu", "Mandatory"),
    "IC":   ("Introducere în contabilitate", "Introduction to Accounting", "Accounting", "Obligatoriu", "Mandatory"),
    "MAT":  ("Matematică", "Mathematics", "Mathematics", "Obligatoriu", "Mandatory"),
    "MIC":  ("Microeconomie", "Microeconomics", "Microeconomics", "Obligatoriu", "Mandatory"),
    "REI":  ("Relații economice internaționale", "International Economic Relations",
             "Intl. Economic Relations", "Obligatoriu", "Mandatory"),
    "ENG":  ("Limba engleză și comunicare de specialitate 1",
             "English Language and Specialised Communication 1", "English", "Opțional", "Optional"),
    "SPO":  ("Educaţie fizică şi sport 1", "Physical Education and Sport 1", "PE & Sport", "Special", "Special"),
}
DAY_RO = ["Luni", "Marți", "Miercuri", "Joi", "Vineri", "Sâmbătă", "Duminică"]
DAY_EN = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


class DocError(Exception):
    """The document could not be read with confidence."""


def fold(s):
    """Lower-case, strip diacritics (ş/ș, ţ/ț, ă, â, î) and collapse spaces."""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", s).strip().lower()


def clean(s):
    return re.sub(r"\s+", " ", s.replace("\xa0", " ")).strip()


SUBJECT_KEYS = {fold(v[0]): k for k, v in SUBJECTS.items()}
SUBJECT_KEYS[fold("Filozofie")] = "FIL"
SUBJECT_KEYS[fold("Educație fizică și sport 1")] = "SPO"
DAY_KEYS = {fold(d): i for i, d in enumerate(DAY_RO)}
DAY_KEYS[fold("Marti")] = 1

HEADING = re.compile(r"^activitate tutoriala\s+(?P<subj>.+?)\s*-\s*(?:grupa\s+)?(?P<group>\d{3})\b")
PLAN = re.compile(r"\(\s*(?P<kind>obligatoriu|optional|special)\s*,\s*(?P<n>\d+)\s+activitati in plan")
DATE = re.compile(r"^(\d{1,2})\s*\.+\s*(\d{1,2})\s*\.+\s*(\d{4})$")
TIME = re.compile(r"^(\d{1,2})\s*[:.]\s*(\d{2})$")


# ---------------------------------------------------------------- reading
class _DocHTML(html.parser.HTMLParser):
    """Turns the published page into a stream of ('p', text) and ('table', rows)."""
    BLOCK = {"p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "div"}
    SKIP = {"script", "style", "head", "title", "noscript"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.blocks, self.buf = [], []
        self.depth = self.skip = 0
        self.rows = self.row = self.cell = None

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1
        elif tag == "table":
            self._flush()
            self.depth += 1
            if self.depth == 1:
                self.rows = []
        elif self.depth == 1 and tag == "tr":
            self.row = []
        elif self.depth == 1 and tag in ("td", "th"):
            self.cell = []
        elif tag == "br" or (tag in self.BLOCK and self.cell is not None):
            (self.cell if self.cell is not None else self.buf).append(" ")
        elif tag in self.BLOCK and self.depth == 0:
            self._flush()

    def handle_endtag(self, tag):
        if tag in self.SKIP:
            self.skip = max(0, self.skip - 1)
        elif self.depth == 1 and tag in ("td", "th") and self.cell is not None:
            self.row.append(clean("".join(self.cell)))
            self.cell = None
        elif self.depth == 1 and tag == "tr" and self.row is not None:
            if any(self.row):
                self.rows.append(self.row)
            self.row = None
        elif tag == "table" and self.depth:
            self.depth -= 1
            if self.depth == 0:
                self.blocks.append(("table", self.rows))
                self.rows = None
        elif tag in self.BLOCK and self.depth == 0:
            self._flush()

    def handle_data(self, data):
        if self.skip:
            return
        if self.depth:
            if self.cell is not None:
                self.cell.append(data)
        else:
            self.buf.append(data)

    def _flush(self):
        text = clean("".join(self.buf))
        self.buf = []
        if text:
            self.blocks.append(("p", text))

    def close(self):
        super().close()
        self._flush()


def blocks_from_html(text):
    p = _DocHTML()
    p.feed(text)
    p.close()
    return p.blocks


def blocks_from_text(text):
    """Text copied from the page: one cell per line, tables start with '#  Data  Ziua ...'."""
    lines = [clean(l) for l in text.splitlines()]
    lines = [l for l in lines if l]
    header = ["#", "data", "ziua", "ora", "sala", "profesor"]
    blocks, i = [], 0
    while i < len(lines):
        if [fold(x) for x in lines[i:i + 6]] == header:
            rows, i = [lines[i:i + 6]], i + 6
            while i + 5 < len(lines) + 0 and re.fullmatch(r"\d{1,2}", lines[i]) and DATE.match(lines[i + 1]):
                rows.append(lines[i:i + 6])
                i += 6
            blocks.append(("table", rows))
        else:
            blocks.append(("p", lines[i]))
            i += 1
    return blocks


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (timetable updater)",
                                               "Cache-Control": "no-cache"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read().decode("utf-8", "replace")


# ---------------------------------------------------------------- interpreting
def interpret(blocks):
    """Returns (sessions, warnings). Raises DocError when anything is unclear."""
    tables, warnings = [], []
    heading = plan = None
    for kind, val in blocks:
        if kind == "p":
            f = fold(val)
            m = HEADING.match(f)
            if m:
                code = SUBJECT_KEYS.get(m.group("subj").strip())
                if not code:
                    raise DocError(f"Unknown subject in heading: “{val}”")
                heading, plan = (code, m.group("group")), None
            pm = PLAN.search(f)
            if pm:
                plan = (pm.group("kind"), int(pm.group("n")))
            continue
        rows = val
        if not rows or not any("data" == fold(c) for c in rows[0]):
            continue  # not a timetable table
        tables.append({"key": heading, "plan": plan, "rows": rows})
        heading = plan = None

    if not tables:
        raise DocError("No timetable tables found on the page (the page may not have loaded correctly).")

    # A table without a heading belongs to the same subject as the next heading,
    # for the group just before it (e.g. untitled table, then "Microeconomie - 169" -> Microeconomie 168),
    # but only if that subject/group is otherwise missing.
    found = {t["key"] for t in tables if t["key"]}
    for i, t in enumerate(tables):
        if t["key"]:
            continue
        nxt = next((u["key"] for u in tables[i + 1:] if u["key"]), None)
        guess = (nxt[0], str(int(nxt[1]) - 1)) if nxt else None
        if not guess or guess[1] not in GROUPS or guess in found:
            raise DocError("A table without a heading could not be matched to a subject and group "
                           f"(table number {i + 1}, first row: {' | '.join(t['rows'][1]) if len(t['rows']) > 1 else 'empty'}).")
        t["key"] = guess
        found.add(guess)
        warnings.append(f"The table for {SUBJECTS[guess[0]][1]} group {guess[1]} has no heading in the document; "
                        "it was matched by its position.")

    seen, sessions = {}, []
    for t in tables:
        code, group = t["key"]
        if group not in GROUPS:
            raise DocError(f"Unknown group {group} in {SUBJECTS[code][0]}.")
        if (code, group) in seen:
            raise DocError(f"{SUBJECTS[code][0]} - {group} appears twice in the document.")
        seen[(code, group)] = True
        head = [fold(c) for c in t["rows"][0]]
        try:
            ci = {k: head.index(k) for k in ("data", "ziua", "ora", "sala", "profesor")}
        except ValueError:
            raise DocError(f"Unexpected columns in {SUBJECTS[code][0]} - {group}: {t['rows'][0]}")
        body = t["rows"][1:]
        if t["plan"] and t["plan"][1] != len(body):
            raise DocError(f"{SUBJECTS[code][0]} - {group}: the document says {t['plan'][1]} activities "
                           f"but the table has {len(body)} rows.")
        for r in body:
            if len(r) < len(head):
                raise DocError(f"{SUBJECTS[code][0]} - {group}: incomplete row {r}")
            d, day, tm, room, teacher = (clean(r[ci[k]]) for k in ("data", "ziua", "ora", "sala", "profesor"))
            dm, tmm = DATE.match(d), TIME.match(tm)
            if not dm:
                raise DocError(f"{SUBJECTS[code][0]} - {group}: unreadable date “{d}”.")
            if not tmm:
                raise DocError(f"{SUBJECTS[code][0]} - {group}: unreadable time “{tm}” on {d}.")
            try:
                date = dt.date(int(dm.group(3)), int(dm.group(2)), int(dm.group(1)))
                h, mi = int(tmm.group(1)), int(tmm.group(2))
                start = dt.datetime(date.year, date.month, date.day, h, mi, tzinfo=TZ)
            except ValueError:
                raise DocError(f"{SUBJECTS[code][0]} - {group}: impossible date/time “{d} {tm}”.")
            if not room or not teacher:
                raise DocError(f"{SUBJECTS[code][0]} - {group}: missing room or teacher on {d} {tm}.")
            note = ""
            wd = DAY_KEYS.get(fold(day))
            if wd is None:
                raise DocError(f"{SUBJECTS[code][0]} - {group}: unknown weekday “{day}” on {d}.")
            if wd != date.weekday():
                note = (f"The university document lists this session as {DAY_RO[wd]} {date:%d.%m.%Y}, "
                        f"but {date:%d.%m.%Y} is a {DAY_EN[date.weekday()]} ({DAY_RO[date.weekday()]}). "
                        f"It is shown here on {DAY_EN[date.weekday()]}; check with the faculty.")
                warnings.append(f"{SUBJECTS[code][1]} group {group}: {note}")
            if d != f"{date:%d.%m.%Y}" and not re.fullmatch(r"\d\.\d{2}\.\d{4}", d):
                warnings.append(f"{SUBJECTS[code][1]} group {group}: date written as “{d}”, read as {date:%d.%m.%Y}.")
            sessions.append({"code": code, "group": group, "start": start.strftime("%Y-%m-%dT%H:%M"),
                             "room": room, "teacher": teacher, "note": note})

    missing = [f"{SUBJECTS[c][0]} - {g}" for c in SUBJECTS for g in GROUPS if (c, g) not in seen]
    if missing:
        raise DocError("These subjects/groups are missing from the document: " + ", ".join(missing))
    sessions.sort(key=lambda s: (s["start"], s["group"], s["code"]))
    return sessions, list(dict.fromkeys(warnings))


# ---------------------------------------------------------------- building
def events_from(sessions):
    evs, count, total = [], {}, {}
    for s in sessions:
        total[(s["code"], s["group"])] = total.get((s["code"], s["group"]), 0) + 1
    for s in sessions:
        st = dt.datetime.strptime(s["start"], "%Y-%m-%dT%H:%M").replace(tzinfo=TZ)
        en = st + dt.timedelta(minutes=DURATION_MIN)
        k = (s["code"], s["group"])
        count[k] = count.get(k, 0) + 1
        ro, eng, short, kro, ken = SUBJECTS[s["code"]]
        evs.append({
            "id": f"{s['code']}-{s['group']}-{st:%Y%m%d%H%M}", "code": s["code"], "group": s["group"],
            "date": f"{st:%Y-%m-%d}", "start": f"{st:%H:%M}", "end": f"{en:%H:%M}",
            "dayEn": DAY_EN[st.weekday()], "dayRo": DAY_RO[st.weekday()],
            "room": s["room"], "teacher": s["teacher"], "ro": ro, "en": eng, "short": short,
            "kindRo": kro, "kindEn": ken, "n": count[k], "of": total[k], "note": s["note"], "clash": [],
            "_st": st, "_en": en})
    for a in evs:
        for b in evs:
            if a is not b and a["group"] == b["group"] and a["_st"] < b["_en"] and b["_st"] < a["_en"]:
                a["clash"].append(b["id"])
    return evs


def _esc(s):
    return s.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def _fold_line(line):
    b, out = line.encode("utf-8"), []
    while len(b) > 73:
        cut = 73
        while (b[cut] & 0xC0) == 0x80:
            cut -= 1
        out.append(b[:cut].decode())
        b = b[cut:]
    out.append(b.decode())
    return "\r\n ".join(out)


def _utc(x):
    return x.astimezone(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def ics_for(group, evs, stamp):
    L = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//orar-man-id//Orar MAN ID//RO", "CALSCALE:GREGORIAN",
         "METHOD:PUBLISH", f"X-WR-CALNAME:Orar Grupa {group} · MAN ID an I\\, sem. 1",
         "X-WR-TIMEZONE:Europe/Bucharest", "REFRESH-INTERVAL;VALUE=DURATION:PT1H", "X-PUBLISHED-TTL:PT1H"]
    for e in (x for x in evs if x["group"] == group):
        desc = (f"EN: {e['en']}\nRO: {e['ro']}\nActivitate tutorială / Tutorial activity {e['n']}/{e['of']}\n"
                f"Profesor / Teacher: {e['teacher']}\nSala / Room: {e['room']}\nGrupa / Group: {group}\n"
                f"Tip / Type: {e['kindRo']} / {e['kindEn']}\n"
                f"Ora de sfârșit estimată (90 min) / End time estimated (90 min)")
        if e["note"]:
            desc += "\nNOTE: " + e["note"]
        if e["clash"]:
            desc += "\nNOTE: overlaps another session of this group in the university document."
        L += ["BEGIN:VEVENT", f"UID:{e['id']}@orar-man-id", f"DTSTAMP:{stamp}",
              f"DTSTART:{_utc(e['_st'])}", f"DTEND:{_utc(e['_en'])}",
              _fold_line("SUMMARY:" + _esc(f"{e['en']} / {e['ro']}")),
              _fold_line("LOCATION:" + _esc(f"Sala {e['room']}")),
              _fold_line("DESCRIPTION:" + _esc(desc)),
              "BEGIN:VALARM", "ACTION:DISPLAY", "DESCRIPTION:Reminder", "TRIGGER:-PT30M", "END:VALARM",
              "END:VEVENT"]
    L.append("END:VCALENDAR")
    return "\r\n".join(L) + "\r\n"


def describe(s):
    st = dt.datetime.strptime(s["start"], "%Y-%m-%dT%H:%M")
    return f"{DAY_EN[st.weekday()][:3]} {st:%d.%m} {st:%H:%M}, Sala {s['room']}"


def diff(old, new):
    """Human-readable list of what moved, per subject and group."""
    key = lambda s: (s["code"], s["group"])
    sig = lambda s: (s["start"], s["room"], s["teacher"])
    out = []
    for g in GROUPS:
        for c in SUBJECTS:
            a = sorted(sig(s) for s in old if key(s) == (c, g))
            b = sorted(sig(s) for s in new if key(s) == (c, g))
            if a == b:
                continue
            gone = [s for s in old if key(s) == (c, g) and sig(s) not in b]
            came = [s for s in new if key(s) == (c, g) and sig(s) not in a]
            line = f"- **Group {g}, {SUBJECTS[c][1]}** ({SUBJECTS[c][0]}): "
            parts = []
            if gone:
                parts.append("removed " + "; ".join(describe(s) for s in gone))
            if came:
                parts.append("added " + "; ".join(describe(s) for s in came))
            out.append(line + " → ".join(parts))
    return out


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(text)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from-html")
    ap.add_argument("--from-text")
    ap.add_argument("--summary")
    ap.add_argument("--url", default=DOC_URL)
    a = ap.parse_args()

    def summary(text):
        if a.summary:
            write(a.summary, text)
        print(text)

    data_path = os.path.join(ROOT, "timetable.json")
    old = {}
    if os.path.exists(data_path):
        with open(data_path, encoding="utf-8") as f:
            old = json.load(f)

    try:
        if a.from_text:
            with open(a.from_text, encoding="utf-8") as f:
                blocks = blocks_from_text(f.read())
        else:
            if a.from_html:
                with open(a.from_html, encoding="utf-8") as f:
                    page = f.read()
            else:
                page = fetch(a.url)
            blocks = blocks_from_html(page)
        sessions, warnings = interpret(blocks)
    except DocError as e:
        summary("## The timetable could not be updated automatically\n\n"
                f"{e}\n\nNothing was changed on the website. Check the university document and fix the "
                "problem (or ask for help), then run the workflow again from the Actions tab.\n\n"
                f"Document: {a.url}\n")
        return 2
    except Exception as e:  # network errors etc.
        summary(f"## The timetable could not be updated automatically\n\nThe document could not be downloaded: "
                f"{type(e).__name__}: {e}\n\nNothing was changed on the website.\n")
        return 2

    changed = sessions != old.get("sessions")
    now = dt.datetime.now(dt.timezone.utc)
    last = now.strftime("%Y-%m-%dT%H:%M:%SZ") if changed or "lastChanged" not in old else old["lastChanged"]
    stamp = last.replace("-", "").replace(":", "")
    evs = events_from(sessions)

    pub = [{k: v for k, v in e.items() if not k.startswith("_")} for e in evs]
    meta = {"lastChanged": last, "warnings": warnings, "source": a.url}
    with open(os.path.join(ROOT, "template.html"), encoding="utf-8") as f:
        tpl = f.read()
    page = (tpl.replace("__EVENTS__", json.dumps(pub, ensure_ascii=False))
               .replace("__META__", json.dumps(meta, ensure_ascii=False)))
    write(os.path.join(ROOT, "index.html"), page)
    for g in GROUPS:
        write(os.path.join(ROOT, f"grupa-{g}.ics"), ics_for(g, evs, stamp))
    write(os.path.join(ROOT, ".nojekyll"), "")
    write(data_path, json.dumps({"lastChanged": last, "sessions": sessions, "warnings": warnings},
                                ensure_ascii=False, indent=1) + "\n")

    lines = diff(old.get("sessions", []), sessions) if old.get("sessions") else []
    text = "## Orar actualizat / Timetable updated\n\n" if changed else "## No changes\n\n"
    if lines:
        text += "Changes found in the university document:\n\n" + "\n".join(lines) + "\n\n"
    elif changed:
        text += f"Timetable built from the document ({len(sessions)} sessions).\n\n"
    if warnings:
        text += "Things to check in the document:\n\n" + "\n".join(f"- {w}" for w in warnings) + "\n\n"
    if changed:
        text += ("People who subscribed to a group calendar get the change automatically (iPhone usually within a "
                 "few hours, Google Calendar within a day). People who imported the file once need to import it again.\n")
    summary(text)
    if a.summary:
        write(a.summary + ".changed", "true" if changed else "false")
    return 0


if __name__ == "__main__":
    sys.exit(main())
