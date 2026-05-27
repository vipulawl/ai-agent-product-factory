"""
AI Agent Product Factory — Dashboard
Run: streamlit run dashboard.py
"""

import json
import sqlite3
import sys
from pathlib import Path
from datetime import datetime

import streamlit as st

sys.path.insert(0, str(Path(__file__).parent))
from storage.backlog import init_db, DB_PATH

st.set_page_config(
    page_title="Agent Factory",
    page_icon="🏭",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── helpers ───────────────────────────────────────────────────────────────────

@st.cache_resource
def get_conn():
    init_db()
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def q(sql, params=()):
    return [dict(r) for r in get_conn().execute(sql, params).fetchall()]


def scalar(sql, params=()):
    row = get_conn().execute(sql, params).fetchone()
    return row[0] if row else 0


def source_badges(breakdown_json: str) -> str:
    bd = json.loads(breakdown_json or "{}")
    colors = {"reddit": "#FF4500", "hackernews": "#FF6600", "github": "#238636"}
    parts = []
    for src, cnt in bd.items():
        color = colors.get(src, "#888")
        parts.append(f'<span style="background:{color};color:white;padding:2px 7px;border-radius:10px;font-size:11px;margin-right:3px">{src} {cnt}</span>')
    return "".join(parts)


def severity_color(s):
    return {"high": "🔴", "medium": "🟡", "low": "🟢"}.get(s, "⚪")


def status_color(s):
    return {"pending": "🔵", "building": "🟡", "built": "🟢", "skipped": "⚫", "growing": "🌱", "mature": "🌳", "ready_to_build": "⚡"}.get(s, "⚪")


def ago(ts):
    if not ts:
        return "—"
    try:
        dt = datetime.fromisoformat(ts)
        diff = datetime.utcnow() - dt
        days = diff.days
        hours = diff.seconds // 3600
        if days > 0:
            return f"{days}d ago"
        if hours > 0:
            return f"{hours}h ago"
        return "just now"
    except Exception:
        return ts[:10]


# ── sidebar nav ───────────────────────────────────────────────────────────────

st.sidebar.title("🏭 Agent Factory")
st.sidebar.caption("AI Developer Pain Point Pipeline")
st.sidebar.divider()

page = st.sidebar.radio(
    "View",
    ["Overview", "Clusters", "Pain Points", "Backlog", "Builds"],
    label_visibility="collapsed"
)

if st.sidebar.button("🔄 Refresh data"):
    st.cache_data.clear()
    st.rerun()

st.sidebar.divider()
total_posts = scalar("SELECT COUNT(*) FROM raw_posts")
total_pp    = scalar("SELECT COUNT(*) FROM pain_points")
total_cl    = scalar("SELECT COUNT(*) FROM clusters")
total_bl    = scalar("SELECT COUNT(*) FROM backlog_items")
total_builds= scalar("SELECT COUNT(*) FROM builds")
st.sidebar.metric("Posts scraped", total_posts)
st.sidebar.metric("Pain points", total_pp)
st.sidebar.metric("Clusters", total_cl)
st.sidebar.metric("Backlog items", total_bl)
st.sidebar.metric("Builds", total_builds)


# ══════════════════════════════════════════════════════════════════════════════
# OVERVIEW
# ══════════════════════════════════════════════════════════════════════════════

if page == "Overview":
    st.title("📊 Overview")

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Posts scraped", total_posts)
    c2.metric("Pain points", total_pp,
              delta=scalar("SELECT COUNT(*) FROM pain_points WHERE date(created_at)=date('now')"),
              delta_color="normal", help="Today's delta")
    c3.metric("Clusters", total_cl,
              delta=scalar("SELECT COUNT(*) FROM clusters WHERE date(last_updated)=date('now')"))
    c4.metric("Ready to build", scalar("SELECT COUNT(*) FROM backlog_items WHERE status='pending' AND priority_score>=6"))
    c5.metric("Builds shipped", total_builds)

    st.divider()

    col_left, col_right = st.columns([3, 2])

    with col_left:
        st.subheader("Top clusters by evidence")
        rows = q("""
            SELECT c.id, c.name, c.evidence_count, c.source_diversity,
                   c.source_breakdown, c.promo_count, c.known_solutions,
                   c.status, c.last_updated,
                   b.priority_score
            FROM clusters c
            LEFT JOIN backlog_items b ON b.cluster_id = c.id
            WHERE c.evidence_count > 0
            ORDER BY c.evidence_count DESC
            LIMIT 15
        """)
        for r in rows:
            with st.container(border=True):
                h1, h2 = st.columns([5, 1])
                with h1:
                    st.markdown(f"**{r['name']}** {status_color(r['status'])}")
                    st.markdown(source_badges(r["source_breakdown"]), unsafe_allow_html=True)
                with h2:
                    st.metric("Evidence", r["evidence_count"])

                detail1, detail2, detail3 = st.columns(3)
                detail1.caption(f"Sources: {r['source_diversity']}")
                promo = r.get("promo_count") or 0
                detail2.caption(f"Promotional: {promo}/{r['evidence_count']}")
                solutions = json.loads(r.get("known_solutions") or "[]")
                detail3.caption(f"Known solutions: {len(solutions)}")

                if solutions:
                    sol_names = ", ".join(s.get("name","?") for s in solutions[:3])
                    st.caption(f"🔧 Existing tools: {sol_names}")

                score = r.get("priority_score")
                if score:
                    st.progress(min(score / 10, 1.0), text=f"Priority: {score:.1f}/10")

    with col_right:
        st.subheader("Source breakdown")
        source_rows = q("""
            SELECT source_type, COUNT(*) as cnt
            FROM pain_points GROUP BY source_type ORDER BY cnt DESC
        """)
        if source_rows:
            st.bar_chart({r["source_type"]: r["cnt"] for r in source_rows})

        st.subheader("Pain point severity")
        sev_rows = q("""
            SELECT severity, COUNT(*) as cnt
            FROM pain_points GROUP BY severity
        """)
        if sev_rows:
            st.bar_chart({(r["severity"] or "unknown"): r["cnt"] for r in sev_rows})

        st.subheader("Cluster status")
        status_rows = q("SELECT status, COUNT(*) as cnt FROM clusters GROUP BY status")
        if status_rows:
            st.bar_chart({r["status"]: r["cnt"] for r in status_rows})

        st.subheader("Posts over time (last 14d)")
        daily = q("""
            SELECT date(created_at) as day, COUNT(*) as cnt
            FROM raw_posts
            WHERE date(created_at) >= date('now', '-14 days')
            GROUP BY day ORDER BY day
        """)
        if daily:
            st.line_chart({r["day"]: r["cnt"] for r in daily})


# ══════════════════════════════════════════════════════════════════════════════
# CLUSTERS
# ══════════════════════════════════════════════════════════════════════════════

elif page == "Clusters":
    st.title("🧩 Clusters")
    st.caption("Each cluster is a synthesised problem theme built up across many scrape runs.")

    col1, col2, col3 = st.columns(3)
    status_filter = col1.selectbox("Status", ["all", "growing", "mature", "ready_to_build", "built"])
    min_evidence = col2.slider("Min evidence", 0, 20, 0)
    sort_by = col3.selectbox("Sort by", ["evidence_count DESC", "last_updated DESC", "source_diversity DESC"])

    where = "WHERE c.evidence_count >= ?"
    params = [min_evidence]
    if status_filter != "all":
        where += " AND c.status = ?"
        params.append(status_filter)

    rows = q(f"""
        SELECT c.*, b.priority_score, b.status as backlog_status
        FROM clusters c
        LEFT JOIN backlog_items b ON b.cluster_id = c.id
        {where}
        ORDER BY c.{sort_by}
    """, params)

    st.caption(f"{len(rows)} clusters")

    for r in rows:
        solutions = json.loads(r.get("known_solutions") or "[]")
        with st.expander(
            f"{status_color(r['status'])}  **{r['name']}**  — {r['evidence_count']} evidence  "
            f"{'  🔧 ' + str(len(solutions)) + ' known solutions' if solutions else ''}",
            expanded=False
        ):
            m1, m2, m3, m4 = st.columns(4)
            m1.metric("Evidence", r["evidence_count"])
            m2.metric("Source types", r["source_diversity"])
            m3.metric("Promotional", r.get("promo_count") or 0)
            score = r.get("priority_score")
            m4.metric("Priority", f"{score:.1f}" if score else "—")

            st.markdown(source_badges(r["source_breakdown"]), unsafe_allow_html=True)
            st.caption(f"First seen: {ago(r['first_seen'])}  ·  Last updated: {ago(r['last_updated'])}")

            if r.get("synthesis_narrative"):
                st.markdown("**Problem brief**")
                st.info(r["synthesis_narrative"])
            else:
                st.caption("_No narrative yet — needs 5+ evidence pieces_")

            if solutions:
                st.markdown("**Known solutions (and why they fall short)**")
                for s in solutions:
                    adequate = s.get("adequate", False)
                    icon = "✅" if adequate else "❌"
                    why = s.get("why_not", "")
                    st.markdown(f"{icon} **{s['name']}** — {why or ('adequate' if adequate else 'inadequate')}")

            # Show sample pain points
            pp_rows = q("""
                SELECT pp.description, pp.severity, pp.source_type,
                       pp.is_promotional, pp.solution_mentioned, pp.comment_signal
                FROM pain_points pp
                WHERE pp.cluster_id = ?
                ORDER BY pp.created_at DESC LIMIT 5
            """, (r["id"],))
            if pp_rows:
                st.markdown("**Sample evidence**")
                for pp in pp_rows:
                    promo_tag = " 📢" if pp["is_promotional"] else ""
                    sol_tag = f" → _{pp['solution_mentioned']}_" if pp.get("solution_mentioned") else ""
                    st.markdown(
                        f"{severity_color(pp['severity'])} [{pp['source_type']}]{promo_tag}  "
                        f"{pp['description']}{sol_tag}"
                    )
                    comments = json.loads(pp.get("comment_signal") or "[]")
                    for c in comments[:2]:
                        st.caption(f"  💬 \"{c}\"")


# ══════════════════════════════════════════════════════════════════════════════
# PAIN POINTS
# ══════════════════════════════════════════════════════════════════════════════

elif page == "Pain Points":
    st.title("📍 Pain Points")
    st.caption("Every extracted complaint, stored atomically. Unassigned = not yet matched to a cluster.")

    f1, f2, f3, f4 = st.columns(4)
    src_filter  = f1.selectbox("Source", ["all", "reddit", "hackernews", "github"])
    sev_filter  = f2.selectbox("Severity", ["all", "high", "medium", "low"])
    promo_filter = f3.selectbox("Type", ["all", "organic only", "promotional only"])
    assigned_filter = f4.selectbox("Assignment", ["all", "assigned", "unassigned"])

    where_parts = ["1=1"]
    params = []

    if src_filter != "all":
        where_parts.append("pp.source_type = ?")
        params.append(src_filter)
    if sev_filter != "all":
        where_parts.append("pp.severity = ?")
        params.append(sev_filter)
    if promo_filter == "organic only":
        where_parts.append("pp.is_promotional = 0")
    elif promo_filter == "promotional only":
        where_parts.append("pp.is_promotional = 1")
    if assigned_filter == "assigned":
        where_parts.append("pp.cluster_id IS NOT NULL")
    elif assigned_filter == "unassigned":
        where_parts.append("pp.cluster_id IS NULL")

    where = " AND ".join(where_parts)
    rows = q(f"""
        SELECT pp.*, c.name as cluster_name
        FROM pain_points pp
        LEFT JOIN clusters c ON c.id = pp.cluster_id
        WHERE {where}
        ORDER BY pp.created_at DESC
        LIMIT 200
    """, params)

    st.caption(f"{len(rows)} pain points (max 200 shown)")

    for r in rows:
        comments = json.loads(r.get("comment_signal") or "[]")
        promo_icon = "📢 " if r["is_promotional"] else ""
        cluster_tag = f" → **{r['cluster_name']}**" if r.get("cluster_name") else " → _unassigned_"

        with st.container(border=True):
            c1, c2 = st.columns([6, 1])
            with c1:
                st.markdown(
                    f"{severity_color(r['severity'])} {promo_icon}"
                    f"[{r['source_type']}] {r['description']}{cluster_tag}"
                )
                if r.get("solution_mentioned"):
                    adequate = "✅ adequate" if r["solution_adequate"] else "❌ inadequate"
                    st.caption(f"🔧 Solution mentioned: **{r['solution_mentioned']}** ({adequate})")
                for c in comments[:2]:
                    st.caption(f"  💬 \"{c}\"")
            with c2:
                st.caption(ago(r["created_at"]))
                if r.get("source_url"):
                    st.markdown(f"[↗]({r['source_url']})")


# ══════════════════════════════════════════════════════════════════════════════
# BACKLOG
# ══════════════════════════════════════════════════════════════════════════════

elif page == "Backlog":
    st.title("📋 Backlog")
    st.caption("Prioritised build queue. Items promoted here once a cluster hits 5+ evidence and gets a narrative.")

    status_filter = st.selectbox("Status", ["all", "pending", "building", "built", "skipped"])

    where = "" if status_filter == "all" else "WHERE b.status = ?"
    params = [] if status_filter == "all" else [status_filter]

    rows = q(f"""
        SELECT b.*, c.evidence_count, c.source_diversity, c.source_breakdown,
               c.known_solutions, c.synthesis_narrative, c.promo_count
        FROM backlog_items b
        JOIN clusters c ON c.id = b.cluster_id
        {where}
        ORDER BY b.priority_score DESC, b.created_at DESC
    """, params)

    for r in rows:
        score = r["priority_score"] or 0
        with st.container(border=True):
            h1, h2, h3 = st.columns([5, 1, 1])
            with h1:
                st.markdown(f"{status_color(r['status'])} **{r['title']}**")
                st.markdown(source_badges(r["source_breakdown"]), unsafe_allow_html=True)
            with h2:
                st.metric("Priority", f"{score:.1f}" if score else "—")
            with h3:
                st.metric("Evidence", r["evidence_count"])

            if score:
                st.progress(min(score / 10, 1.0))

            m1, m2, m3, m4, m5 = st.columns(5)
            m1.caption(f"Freq: {r['frequency_score'] or '—'}")
            m2.caption(f"Novelty: {r['novelty_score'] or '—'}")
            m3.caption(f"Feasibility: {r['feasibility_score'] or '—'}")
            m4.caption(f"Recency: {r['recency_score'] or '—'}")
            m5.caption(f"Market: {r['market_score'] or '—'}")

            solutions = json.loads(r.get("known_solutions") or "[]")
            if solutions:
                sol_str = ", ".join(f"{s['name']} ({'✅' if s.get('adequate') else '❌'})" for s in solutions)
                st.caption(f"🔧 Known solutions: {sol_str}")

            if r.get("notes"):
                st.caption(f"📝 {r['notes']}")

            if r.get("synthesis_narrative"):
                with st.expander("Problem brief"):
                    st.write(r["synthesis_narrative"])

            st.caption(f"Added {ago(r['created_at'])}  ·  Updated {ago(r['updated_at'])}")


# ══════════════════════════════════════════════════════════════════════════════
# BUILDS
# ══════════════════════════════════════════════════════════════════════════════

elif page == "Builds":
    st.title("🚀 Builds")
    st.caption("Products shipped to GitHub by the builder agent.")

    rows = q("""
        SELECT bld.*, bi.title, bi.priority_score,
               c.evidence_count, c.source_breakdown, c.synthesis_narrative
        FROM builds bld
        JOIN backlog_items bi ON bi.id = bld.backlog_item_id
        JOIN clusters c ON c.id = bi.cluster_id
        ORDER BY bld.created_at DESC
    """)

    if not rows:
        st.info("No builds yet. Set up your API keys and run the builder agent to get started.")
    else:
        for r in rows:
            with st.container(border=True):
                c1, c2 = st.columns([5, 2])
                with c1:
                    st.markdown(f"### [{r['repo_name']}]({r['github_url']})")
                    st.caption(f"Solving: {r['title']}")
                    st.markdown(source_badges(r["source_breakdown"]), unsafe_allow_html=True)
                with c2:
                    st.metric("Evidence base", r["evidence_count"])
                    st.caption(f"Built {ago(r['created_at'])}")
                    notified = "✅ notified" if r["notified"] else "⏳ pending"
                    st.caption(f"Telegram: {notified}")

                if r.get("synthesis_narrative"):
                    with st.expander("Original problem brief"):
                        st.write(r["synthesis_narrative"])

                if r.get("build_summary"):
                    try:
                        summary = json.loads(r["build_summary"])
                        files = summary.get("design", {}).get("files", [])
                        if files:
                            st.caption(f"Files generated: {', '.join(files[:6])}" +
                                       (f" +{len(files)-6} more" if len(files) > 6 else ""))
                    except Exception:
                        pass
