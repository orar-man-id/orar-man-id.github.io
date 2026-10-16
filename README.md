# Orar MAN ID, an I, sem. 1, 2026–2027

Interactive timetable for groups 168–172: https://orar-man-id.github.io

Every hour, a GitHub Action (`.github/workflows/update.yml`) reads the university's published document,
rebuilds the page and the per-group calendar files (`site/grupa-168.ics` … `grupa-172.ics`) and publishes
them if anything changed. It opens an issue listing the changes. If the document can't be read with
confidence (missing table, unreadable date, page not loading), it changes nothing and opens an issue
labelled `update-failed` instead.

- `scripts/update.py` reads, checks and builds everything. Run `python scripts/update.py --from-text copy.txt`
  to build from text copied from the document instead of downloading it.
- `scripts/template.html` is the page.
- `data/timetable.json` is the last timetable that was published, used to detect changes.

Start times, rooms and teachers come from the university document; end times assume 90-minute sessions.
