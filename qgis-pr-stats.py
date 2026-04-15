#!/usr/bin/env python3
"""
PR review statistics for qgis/QGIS.

Fetches: all open PRs + PRs merged in the last N months.
Requires: gh CLI (https://cli.github.com/) authenticated with `gh auth login`
Outputs: Markdown to stdout, HTML file, progress to stderr
"""

import json
import shutil
import subprocess
import sys
from datetime import datetime, timedelta

USERS = ["3nids", "nirvn", "m-kuhn", "signedav", "ValentinBuira", "gacarrillor"]
NUM_MONTHS = 3

# GraphQL search query — GitHub search supports is:open, is:merged, merged:>date
SEARCH_QUERY = """
query($q: String!, $cursor: String) {
  search(query: $q, type: ISSUE, first: 50, after: $cursor) {
    pageInfo { hasNextPage endCursor }
    nodes {
      ... on PullRequest {
        number title url state merged mergedAt createdAt
        mergedBy { login }
        reviews(first: 50) { nodes { author { login } } }
        comments(first: 50) { nodes { author { login } } }
        reviewThreads(first: 50) {
          nodes { comments(first: 10) { nodes { author { login } } } }
        }
      }
    }
  }
}
"""


def gh_gql(query, variables=None):
    cmd = ["gh", "api", "graphql", "-f", f"query={query}"]
    for k, v in (variables or {}).items():
        cmd += ["-f", f"{k}={v}"]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f"gh error: {r.stderr.strip()}")
    data = json.loads(r.stdout)
    if "errors" in data:
        sys.exit(f"GraphQL errors: {json.dumps(data['errors'], indent=2)}")
    return data


def fetch_prs(search_q, label):
    """Paginate through a GitHub search query and return all PR nodes."""
    prs = []
    cursor = None
    while True:
        variables = {"q": search_q}
        if cursor:
            variables["cursor"] = cursor
        data = gh_gql(SEARCH_QUERY, variables)
        conn = data["data"]["search"]
        for node in conn["nodes"]:
            if node.get("number"):  # skip empty nodes
                prs.append(node)
        print(f"  [{label}] {len(prs)} PRs fetched...", file=sys.stderr)
        if not conn["pageInfo"]["hasNextPage"]:
            break
        cursor = conn["pageInfo"]["endCursor"]
    return prs


def login(node):
    return ((node.get("author") or {}).get("login") or "").lower()


def main():
    if not shutil.which("gh"):
        sys.exit("Error: 'gh' CLI not found. Install from https://cli.github.com/")

    # Last NUM_MONTHS months + current month
    now = datetime.now()
    first_of_current = now.replace(day=1)
    # Go back exactly NUM_MONTHS months
    m = first_of_current.month - NUM_MONTHS
    y = first_of_current.year
    while m < 1:
        m += 12
        y -= 1
    start_month = first_of_current.replace(year=y, month=m)
    merged_since = start_month.strftime("%Y-%m-%d")

    # Build month labels: NUM_MONTHS previous months + current
    months = []
    d = start_month
    while d <= first_of_current:
        months.append(d.strftime("%Y-%m"))
        if d.month == 12:
            d = d.replace(year=d.year + 1, month=1)
        else:
            d = d.replace(month=d.month + 1)

    print(f"Fetching open PRs + PRs merged since {merged_since} ({', '.join(months)})...", file=sys.stderr)

    # Two searches: open PRs + recently merged PRs
    open_prs = fetch_prs("repo:qgis/QGIS is:pr is:open", "open")
    merged_prs = fetch_prs(f"repo:qgis/QGIS is:pr is:merged merged:>{merged_since}", "merged")

    # Dedupe by PR number
    seen = set()
    prs = []
    for pr in open_prs + merged_prs:
        if pr["number"] not in seen:
            seen.add(pr["number"])
            prs.append(pr)

    print(f"Total: {len(prs)} unique PRs. Analyzing...", file=sys.stderr)

    # Process each PR
    results = []
    for pr in prs:
        merged_by = (pr.get("mergedBy") or {}).get("login", "").lower()
        state = "merged" if pr.get("merged") else pr.get("state", "").lower()

        # Count formal reviews per user
        reviews = {}
        for r in pr["reviews"]["nodes"]:
            l = login(r)
            reviews[l] = reviews.get(l, 0) + 1

        # Count comments: issue-level + inline review comments
        comments = {}
        for c in pr["comments"]["nodes"]:
            l = login(c)
            comments[l] = comments.get(l, 0) + 1
        for t in pr["reviewThreads"]["nodes"]:
            for c in t["comments"]["nodes"]:
                l = login(c)
                comments[l] = comments.get(l, 0) + 1

        # Assign month: use mergedAt for merged PRs, createdAt for open
        date_str = pr.get("mergedAt") or pr.get("createdAt") or ""
        month = date_str[:7] if date_str else ""

        row = {"num": pr["number"], "title": pr["title"], "url": pr["url"],
               "state": state, "month": month, "u": {}}
        dominated = False
        for u in USERS:
            ul = u.lower()
            nc = comments.get(ul, 0)
            nr = reviews.get(ul, 0)
            mg = merged_by == ul
            row["u"][u] = [nc, nr, mg]
            if nc or nr or mg:
                dominated = True

        if dominated:
            results.append(row)

    results.sort(key=lambda r: r["num"], reverse=True)
    print(f"{len(results)} PRs with activity from tracked users.\n", file=sys.stderr)

    # Markdown output
    lines = []
    lines.append("# QGIS PR Review Statistics\n")
    lines.append(f"*Generated {datetime.now().strftime('%Y-%m-%d')} — "
                 f"open PRs + merged since {merged_since}*\n")

    # Build header: PR | State | user1 | user2 | ...
    # Each user cell packs: comments/reviews/merged
    hdr = "| PR | State |"
    sep = "|:---|:---:|"
    for u in USERS:
        hdr += f" @{u} |"
        sep += ":---:|"
    lines.append(hdr)
    lines.append(sep)

    for r in results:
        title_esc = r["title"].replace("|", "\\|")
        pr_cell = f'[#{r["num"]}]({r["url"]}) {title_esc}'
        state_cell = "✅ merged" if r["state"] == "merged" else "🟢 open"
        row = f"| {pr_cell} | {state_cell} |"
        for u in USERS:
            nc, nr, mg = r["u"][u]
            parts = []
            if nc:
                parts.append(f"{nc}💬")
            if nr:
                parts.append(f"{nr}👁")
            if mg:
                parts.append("🔀")
            row += f' {" ".join(parts)} |'
        lines.append(row)

    # Summary
    lines.append("")
    lines.append("**Legend:** 💬 comments · 👁 reviews · 🔀 merged the PR")
    lines.append("")

    md = "\n".join(lines)
    print(md)

    # Write .md file
    md_path = "qgis-pr-stats.md"
    with open(md_path, "w") as f:
        f.write(md)
    print(f"\nWritten to {md_path}", file=sys.stderr)

    # Generate HTML file with interactive filters
    html_data = json.dumps(results, default=str)
    users_json = json.dumps(USERS)
    months_json = json.dumps(months)

    html_doc = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<title>QGIS PR Review Statistics</title>
<style>
@media print {{ @page {{ size: landscape; margin: 1cm; }} #filters {{ display: none; }} }}
* {{ box-sizing: border-box; }}
body {{ font-family: -apple-system, Helvetica, Arial, sans-serif; margin: 0; padding: 1.5em; font-size: 12px; color: #24292e; }}
h1 {{ font-size: 20px; margin: 0 0 4px; }}
.subtitle {{ color: #586069; margin-bottom: 1em; }}
#filters {{ display: flex; flex-wrap: wrap; gap: 12px; align-items: center; margin-bottom: 1em; padding: 10px 14px; background: #f6f8fa; border: 1px solid #e1e4e8; border-radius: 6px; }}
#filters label {{ font-weight: 600; font-size: 11px; text-transform: uppercase; color: #586069; }}
#filters select, #filters input {{ padding: 4px 8px; border: 1px solid #d1d5da; border-radius: 4px; font-size: 12px; }}
#filters input[type=text] {{ width: 200px; }}
.filter-group {{ display: flex; align-items: center; gap: 4px; }}
.chip {{ display: inline-block; padding: 2px 8px; margin: 1px; border-radius: 10px; font-size: 11px; cursor: pointer; border: 1px solid #d1d5da; background: #fff; user-select: none; }}
.chip.active {{ background: #0366d6; color: #fff; border-color: #0366d6; }}
#stats {{ margin-bottom: 8px; color: #586069; font-size: 11px; }}
table {{ border-collapse: collapse; width: 100%; }}
th, td {{ border: 1px solid #e1e4e8; padding: 5px 8px; text-align: left; white-space: nowrap; }}
th {{ background: #f6f8fa; position: sticky; top: 0; z-index: 1; font-size: 11px; }}
td:first-child {{ white-space: normal; max-width: 450px; overflow: hidden; text-overflow: ellipsis; }}
tr:hover {{ background: #f1f8ff; }}
tr.merged {{ }}
tr.open {{ }}
a {{ color: #0366d6; text-decoration: none; }}
a:hover {{ text-decoration: underline; }}
.badge {{ display: inline-block; padding: 1px 6px; border-radius: 8px; font-size: 10px; font-weight: 600; }}
.badge.merged {{ background: #6f42c1; color: #fff; }}
.badge.open {{ background: #28a745; color: #fff; }}
.cell-activity {{ font-size: 11px; }}
.dim {{ opacity: 0.25; }}
</style>
</head><body>

<h1>QGIS PR Review Statistics</h1>
<div class="subtitle">Open PRs + merged since {merged_since} &mdash; generated {now.strftime('%Y-%m-%d')}</div>

<div id="filters">
  <div class="filter-group">
    <label>State:</label>
    <select id="fState"><option value="">All</option><option value="open">Open</option><option value="merged">Merged</option></select>
  </div>
  <div class="filter-group">
    <label>Search:</label>
    <input type="text" id="fSearch" placeholder="PR title or number...">
  </div>
  <div class="filter-group">
    <label>Users:</label>
    <span id="userChips"></span>
  </div>
  <div class="filter-group">
    <label>Month:</label>
    <span id="monthChips"></span>
  </div>
  <div class="filter-group">
    <label>Activity:</label>
    <select id="fActivity">
      <option value="">Any</option>
      <option value="comments">Has comments</option>
      <option value="reviews">Has reviews</option>
      <option value="merged_by">Merged by</option>
    </select>
  </div>
</div>
<div id="stats"></div>
<table>
  <thead><tr id="thead"></tr></thead>
  <tbody id="tbody"></tbody>
</table>

<p style="margin-top:1em;color:#586069;font-size:11px;">
  <b>Legend:</b> 💬 comments &middot; 👁 reviews &middot; 🔀 merged the PR
</p>

<script>
const DATA = {html_data};
const USERS = {users_json};
const MONTHS = {months_json};

let activeUsers = new Set(USERS.map(u => u.toLowerCase()));
let activeMonths = new Set(MONTHS);

function initChips() {{
  const userContainer = document.getElementById('userChips');
  USERS.forEach(u => {{
    const chip = document.createElement('span');
    chip.className = 'chip active';
    chip.textContent = '@' + u;
    chip.dataset.user = u.toLowerCase();
    chip.onclick = () => {{
      chip.classList.toggle('active');
      if (chip.classList.contains('active')) activeUsers.add(u.toLowerCase());
      else activeUsers.delete(u.toLowerCase());
      render();
    }};
    userContainer.appendChild(chip);
  }});

  const monthContainer = document.getElementById('monthChips');
  MONTHS.forEach(m => {{
    const chip = document.createElement('span');
    chip.className = 'chip active';
    // Nice label: "Jan 2026" etc.
    const [y, mo] = m.split('-');
    const label = new Date(parseInt(y), parseInt(mo) - 1).toLocaleString('en', {{month: 'short', year: 'numeric'}});
    chip.textContent = label;
    chip.dataset.month = m;
    chip.onclick = () => {{
      chip.classList.toggle('active');
      if (chip.classList.contains('active')) activeMonths.add(m);
      else activeMonths.delete(m);
      render();
    }};
    monthContainer.appendChild(chip);
  }});
}}

function buildHeader() {{
  const tr = document.getElementById('thead');
  tr.innerHTML = '<th>PR</th><th>State</th>';
  USERS.forEach(u => {{
    const th = document.createElement('th');
    th.textContent = '@' + u;
    const ul = u.toLowerCase();
    if (!activeUsers.has(ul)) th.classList.add('dim');
    tr.appendChild(th);
  }});
}}

function render() {{
  const fState = document.getElementById('fState').value;
  const fSearch = document.getElementById('fSearch').value.toLowerCase();
  const fActivity = document.getElementById('fActivity').value;

  buildHeader();
  const tbody = document.getElementById('tbody');
  tbody.innerHTML = '';
  let count = 0;

  DATA.forEach(r => {{
    // State filter
    if (fState && r.state !== fState) return;

    // Search filter
    if (fSearch && !r.title.toLowerCase().includes(fSearch) && !String(r.num).includes(fSearch)) return;

    // Month filter
    if (r.month && !activeMonths.has(r.month)) return;

    // User/activity filter: PR must have activity from at least one active user
    let hasActivity = false;
    for (const u of USERS) {{
      const ul = u.toLowerCase();
      if (!activeUsers.has(ul)) continue;
      const [nc, nr, mg] = r.u[u];
      if (fActivity === 'comments' && nc > 0) {{ hasActivity = true; break; }}
      else if (fActivity === 'reviews' && nr > 0) {{ hasActivity = true; break; }}
      else if (fActivity === 'merged_by' && mg) {{ hasActivity = true; break; }}
      else if (!fActivity && (nc || nr || mg)) {{ hasActivity = true; break; }}
    }}
    if (!hasActivity) return;

    const tr = document.createElement('tr');
    tr.className = r.state;

    // PR cell
    const tdPR = document.createElement('td');
    tdPR.innerHTML = '<a href="' + r.url + '" target="_blank">#' + r.num + '</a> ' + escHtml(r.title);
    tr.appendChild(tdPR);

    // State cell
    const tdState = document.createElement('td');
    tdState.innerHTML = r.state === 'merged'
      ? '<span class="badge merged">merged</span>'
      : '<span class="badge open">open</span>';
    tr.appendChild(tdState);

    // User cells
    USERS.forEach(u => {{
      const td = document.createElement('td');
      td.className = 'cell-activity';
      const ul = u.toLowerCase();
      if (!activeUsers.has(ul)) {{ td.classList.add('dim'); tr.appendChild(td); return; }}
      const [nc, nr, mg] = r.u[u];
      const parts = [];
      if (nc) parts.push(nc + '💬');
      if (nr) parts.push(nr + '👁');
      if (mg) parts.push('🔀');
      td.textContent = parts.join(' ');
      tr.appendChild(td);
    }});

    tbody.appendChild(tr);
    count++;
  }});

  document.getElementById('stats').textContent = count + ' / ' + DATA.length + ' PRs shown';
}}

function escHtml(s) {{
  const d = document.createElement('div');
  d.textContent = s;
  return d.innerHTML;
}}

document.getElementById('fState').onchange = render;
document.getElementById('fSearch').oninput = render;
document.getElementById('fActivity').onchange = render;

initChips();
render();
</script>
</body></html>"""

    html_path = "qgis-pr-stats.html"
    with open(html_path, "w") as f:
        f.write(html_doc)
    print(f"Written to {html_path} (open in browser → Print → Save as PDF)", file=sys.stderr)


if __name__ == "__main__":
    main()
