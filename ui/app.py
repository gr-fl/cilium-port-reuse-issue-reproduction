import os
import sqlite3
from datetime import datetime
from flask import Flask, render_template_string

app = Flask(__name__)
DB_PATH = os.environ.get("DB_PATH", "/data/cilium_repro.db")

TEMPLATE = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta http-equiv="refresh" content="5">
  <title>Cilium Repro</title>
  <style>
    body { font-family: monospace; background: #111; color: #ccc; padding: 20px; }
    h2 { color: #fff; margin-top: 30px; }
    .stats { display: flex; gap: 20px; margin-bottom: 20px; }
    .stat { background: #1e1e1e; border: 1px solid #333; padding: 15px 25px; border-radius: 6px; }
    .stat .val { font-size: 2em; color: #4fc; }
    .stat .lbl { font-size: 0.8em; color: #888; }
    .hit .val { color: #f64; }
    table { border-collapse: collapse; width: 100%; margin-bottom: 30px; }
    th { background: #222; color: #aaa; text-align: left; padding: 6px 10px; border-bottom: 1px solid #444; }
    td { padding: 5px 10px; border-bottom: 1px solid #222; }
    tr:hover td { background: #1a1a1a; }
    .reuse { color: #f64; font-weight: bold; }
    .tw { color: #4fc; }
    .ts { color: #888; font-size: 0.85em; }
    .gap-warn { color: #fa0; }
    .gap-bad  { color: #f64; font-weight: bold; }
    footer { color: #444; font-size: 0.8em; margin-top: 20px; }
  </style>
</head>
<body>
  <h1 style="color:#fff">Cilium TIME_WAIT / SNAT Repro</h1>
  <p class="ts">Refreshes every 5s &mdash; {{ now }}</p>

  <div class="stats">
    <div class="stat">
      <div class="val">{{ stats.total }}</div>
      <div class="lbl">total connections</div>
    </div>
    <div class="stat">
      <div class="val tw">{{ stats.with_tw }}</div>
      <div class="lbl">TIME_WAIT recorded</div>
    </div>
    <div class="stat {{ 'hit' if stats.reuse_ports > 0 else '' }}">
      <div class="val">{{ stats.reuse_ports }}</div>
      <div class="lbl">port reuse candidates</div>
    </div>
    <div class="stat {{ 'hit' if stats.early_reuse > 0 else '' }}">
      <div class="val">{{ stats.early_reuse }}</div>
      <div class="lbl">reuses &lt; 60s (bug hits)</div>
    </div>
  </div>

  <details {% if stats.early_reuse > 0 %}open{% endif %}>
  <summary style="cursor:pointer;color:#fff;font-size:1.2em;margin:20px 0 10px">
    Port reuse events
    {% if stats.early_reuse > 0 %}<span style="color:#f64"> ({{ stats.early_reuse }} bug hits)</span>{% endif %}
    {% if reuse_events %}<span class="ts"> &mdash; {{ reuse_events|length }} total</span>{% endif %}
  </summary>
  {% if reuse_events %}
  <table>
    <tr><th>src_port</th><th>TIME_WAIT at</th><th>reused at</th><th>gap (s)</th></tr>
    {% for r in reuse_events %}
    <tr>
      <td class="reuse">{{ r.src_port }}</td>
      <td class="ts">{{ r.tw_at }}</td>
      <td class="ts">{{ r.reused_at }}</td>
      <td class="{{ 'gap-bad' if r.gap < 60 else 'gap-warn' if r.gap < 120 else '' }}">{{ "%.1f"|format(r.gap) }}</td>
    </tr>
    {% endfor %}
  </table>
  {% else %}
  <p class="ts">None yet &mdash; waiting for port reuse...</p>
  {% endif %}
  </details>

  <h2>Recent connections (last 30)</h2>
  <table>
    <tr><th>id</th><th>src_port</th><th>accepted</th><th>TIME_WAIT</th><th>released</th></tr>
    {% for c in recent %}
    <tr>
      <td class="ts">{{ c.id }}</td>
      <td>{{ c.src_port }}</td>
      <td class="ts">{{ c.accepted_at }}</td>
      <td class="tw ts">{{ c.time_wait_at or '—' }}</td>
      <td class="ts">{{ c.released_at or '—' }}</td>
    </tr>
    {% endfor %}
  </table>

  <footer>DB: {{ db_path }} &mdash; ss_snapshots: {{ stats.snapshots }}</footer>
</body>
</html>
"""


def fmt_ts(ts):
    if ts is None:
        return None
    return datetime.utcfromtimestamp(ts).strftime("%H:%M:%S.%f")[:-3]


def query(db):
    stats = {}
    stats["total"] = db.execute("SELECT count(*) FROM connections").fetchone()[0]
    stats["with_tw"] = db.execute(
        "SELECT count(*) FROM connections WHERE time_wait_at IS NOT NULL"
    ).fetchone()[0]
    stats["snapshots"] = db.execute("SELECT count(*) FROM ss_snapshots").fetchone()[0]

    reuse_rows = db.execute("""
        SELECT a.src_port,
               a.time_wait_at AS tw_ts,
               b.accepted_at  AS reuse_ts,
               b.accepted_at - a.time_wait_at AS gap
        FROM connections a
        JOIN connections b ON a.src_port = b.src_port AND b.id > a.id
        WHERE a.time_wait_at IS NOT NULL
          AND b.accepted_at > a.time_wait_at
        ORDER BY gap
    """).fetchall()

    reuse_events = [
        {
            "src_port": r[0],
            "tw_at": fmt_ts(r[1]),
            "reused_at": fmt_ts(r[2]),
            "gap": r[3],
        }
        for r in reuse_rows
    ]

    stats["reuse_ports"] = len({r["src_port"] for r in reuse_events})
    stats["early_reuse"] = sum(1 for r in reuse_events if r["gap"] < 60)

    recent_rows = db.execute(
        "SELECT id, src_port, accepted_at, time_wait_at, released_at "
        "FROM connections ORDER BY id DESC LIMIT 30"
    ).fetchall()

    recent = [
        {
            "id": r[0],
            "src_port": r[1],
            "accepted_at": fmt_ts(r[2]),
            "time_wait_at": fmt_ts(r[3]),
            "released_at": fmt_ts(r[4]),
        }
        for r in recent_rows
    ]

    return stats, reuse_events, recent


@app.route("/")
def index():
    db = sqlite3.connect(DB_PATH)
    try:
        stats, reuse_events, recent = query(db)
    finally:
        db.close()

    return render_template_string(
        TEMPLATE,
        stats=stats,
        reuse_events=reuse_events,
        recent=recent,
        now=datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC"),
        db_path=DB_PATH,
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8001, debug=False)
