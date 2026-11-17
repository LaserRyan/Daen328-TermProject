# Stack Exchange Dashboard: the five "revised" pages, reading the pipeline database
#
# Tables (loaded by fill.py / update.py): questions, answers, tags, question_tags,
# tag_monthly_counts.
#
#   1. Tag trends        tag_monthly_counts (tracked tags + site, full history)
#   2. Post activity     tag_monthly_counts (site) + monthly aggregates of questions/answers
#   3. Tag relationships question_tags + questions
#   4. Tag attention     questions + question_tags
#   5. Response time     questions + answers + question_tags
#
# Save next to db.py and run:  streamlit run stack_exchange_dashboard.py
# Extra packages beyond requirements.txt: streamlit plotly networkx scipy

import math
from datetime import datetime, time, timedelta, timezone

import networkx as nx
import numpy as np
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots
from sqlalchemy import text

from db import get_engine as build_engine  # same .env logic as the pipeline (Neon or local)

st.set_page_config(page_title="Stack Exchange Dashboard", layout="wide")


# ============================================================
# DATABASE HELPERS
# ============================================================

@st.cache_resource
def get_engine():
    return build_engine()


def _read(sql, params=None):
    with get_engine().connect() as conn:
        return pd.read_sql(text(sql), conn, params=params)


# Small query results (cached copy per call).
run_query = st.cache_data(ttl=600)(_read)


def check_database():
    """Stop with a helpful message if the pipeline tables are missing or empty."""
    try:
        n = int(run_query("SELECT COUNT(*) AS n FROM public.questions;").loc[0, "n"])
    except Exception as e:
        st.error(
            "Could not read the pipeline tables. Check the database settings in "
            ".env and that `fill` has been run at least once."
        )
        st.caption(str(e).strip().splitlines()[0][:300])
        st.stop()
    if n == 0:
        st.warning("The questions table is empty. Run `docker compose run --rm fill` first.")
        st.stop()


def utc_range(start_date, end_date):
    """Inclusive date range -> [start, end) timestamps in UTC."""
    start = datetime.combine(start_date, time.min, tzinfo=timezone.utc)
    end = datetime.combine(
        end_date + timedelta(days=1), time.min, tzinfo=timezone.utc
    )
    return start, end


def read_date_range(selection, fallback_start, fallback_end):
    """st.date_input returns a partial tuple while the user is picking."""
    if isinstance(selection, (tuple, list)):
        if len(selection) == 2:
            return selection[0], selection[1]
        if len(selection) == 1:
            return selection[0], selection[0]
        return fallback_start, fallback_end
    return selection, selection


def get_question_dates():
    row = run_query(
        """
        SELECT
            MIN(creation_date AT TIME ZONE 'UTC')::date AS min_date,
            MAX(creation_date AT TIME ZONE 'UTC')::date AS max_date
        FROM public.questions;
        """
    ).loc[0]
    return pd.Timestamp(row["min_date"]).date(), pd.Timestamp(row["max_date"]).date()


# Large row-level frames are cached as shared, read-only objects (no per-run
# copy). Pages must not modify them in place.

@st.cache_resource(ttl=600)
def load_attention_questions():
    df = _read(
        """
        SELECT question_id, score, view_count, comment_count, creation_date,
               is_closed, is_duplicate_closure
        FROM public.questions;
        """
    )
    df["creation_date"] = pd.to_datetime(df["creation_date"], utc=True)
    return df


@st.cache_resource(ttl=600)
def load_question_tags():
    return _read("SELECT question_id, tag_name FROM public.question_tags;")


# Response times that are zero (or slightly negative, e.g. a self-answer posted
# at the same moment) are raised to one minute so they fit on a log axis.
MIN_HOURS = 1 / 60

# Response-time column -> flag for times raised to MIN_HOURS.
CLIPPED_COLS = {
    "hours_to_first": "first_clipped",
    "hours_to_accepted": "accepted_clipped"
}


@st.cache_resource(ttl=600)
def load_response_questions():

    """
    One row per question with its response times:

        hours_to_first     hours until the earliest pulled answer
        hours_to_accepted  hours until the accepted answer was POSTED (the
                           schema does not record when it was accepted)
        first_clipped /    True when the raw time was under one minute
        accepted_clipped   (including zero or negative) and was raised
        accepted_missing   True when the question has an accepted answer ID
                           but that answer was never pulled
        answers_incomplete True when fewer answers were pulled than the
                           question's answer_count
    """

    q = _read(
        """
        SELECT
            q.question_id, q.creation_date, q.accepted_answer_id, q.answer_count,
            q.is_closed, q.is_duplicate_closure,
            a.first_answer_date, a.accepted_date,
            COALESCE(a.pulled_answers, 0) AS pulled_answers
        FROM public.questions q
        LEFT JOIN (
            SELECT
                question_id,
                MIN(creation_date) AS first_answer_date,
                MIN(creation_date) FILTER (WHERE is_accepted) AS accepted_date,
                COUNT(*) AS pulled_answers
            FROM public.answers
            GROUP BY question_id
        ) a ON a.question_id = q.question_id;
        """
    )

    for col in ["creation_date", "first_answer_date", "accepted_date"]:
        q[col] = pd.to_datetime(q[col], utc=True)

    for hours_col, date_col in [
        ("hours_to_first", "first_answer_date"),
        ("hours_to_accepted", "accepted_date")
    ]:

        hours = (
            (q[date_col] - q["creation_date"])
            .dt.total_seconds() / 3600
        )

        q[CLIPPED_COLS[hours_col]] = hours < MIN_HOURS
        q[hours_col] = hours.clip(lower=MIN_HOURS)

    q["accepted_missing"] = (
        q["accepted_answer_id"].notna() & q["accepted_date"].isna()
    )

    q["answers_incomplete"] = q["answer_count"] > q["pulled_answers"]

    for flag in ["is_closed", "is_duplicate_closure"]:
        q[flag] = q[flag].fillna(False).astype(bool)

    return q


# Question status filter label -> boolean column that is excluded.
STATUS_FILTERS = {
    "All questions": None,
    "Exclude duplicate closures": "is_duplicate_closure",
    "Open questions only": "is_closed"
}

# Events marked on the activity and response-time charts.
ACTIVITY_EVENTS = {
    "2022-11-30": {"label": "ChatGPT Public Launch", "color": "#E45756"},
    "2023-03-14": {"label": "GPT-4 Release", "color": "#F2A541"},
    "2023-03-23": {"label": "ChatGPT Plugins Announcement", "color": "#54A24B"},
    "2023-08-28": {"label": "ChatGPT Enterprise Launch", "color": "#8064A2"}
}


# ============================================================
# PAGE 1: TAG TRENDS
# ============================================================

GROW_COLOR = "#54A24B"
SHRINK_COLOR = "#E45756"

TREND_METRICS = [
    "Question count",
    "Share of site (%)",
    "Indexed to 100 at start",
    "Month-over-month change (%)"
]


def render_tag_trends():

    st.header("Which tags are growing or shrinking?")

    st.write(
        "Compare recent question volume to an earlier period to find "
        "the fastest-growing and fastest-shrinking Stack Exchange tags."
    )

    st.caption(
        "Monthly counts exist for the tags tracked by the pipeline "
        "(SE_TRACKED_TAGS) and for the whole site."
    )

    df = run_query(
        """
        SELECT scope AS tag, month, question_count, is_partial_month
        FROM public.tag_monthly_counts;
        """
    )

    if df.empty:
        st.info("No monthly tag counts found. Run `fill` to pull them.")
        return

    df["month"] = (
        pd.to_datetime(df["month"])
        .dt.to_period("M")
        .dt.to_timestamp()
    )

    df["is_partial_month"] = df["is_partial_month"].astype(bool)

    # Months flagged as incomplete in the source data.
    partial_months = set(df.loc[df["is_partial_month"], "month"])

    # Separate the site-wide total from the individual tags.
    is_site = df["tag"].str.lower() == "site"

    site_counts = (
        df[is_site].groupby("month")["question_count"].sum()
    )

    tag_df = df[~is_site]

    if tag_df.empty:
        st.info("No per-tag monthly counts found.")
        return

    # One column per tag, one row per month. Missing months become 0
    # so gaps are not silently bridged by the line chart.
    pivot = tag_df.pivot_table(
        index="month",
        columns="tag",
        values="question_count",
        aggfunc="sum"
    )

    all_months = pd.date_range(
        pivot.index.min(), pivot.index.max(), freq="MS"
    )

    pivot = pivot.reindex(all_months).fillna(0)

    site_counts = site_counts.reindex(all_months)

    has_site = site_counts.notna().any() and (site_counts > 0).any()

    # ---- Sidebar filters ----

    st.sidebar.header("Filters")

    include_partial = st.sidebar.checkbox(
        "Include partial months",
        value=False,
        key="tt_partial",
        help=(
            "The most recent month is often incomplete, which makes "
            "every tag look like it is shrinking."
        )
    )

    months_to_drop = (
        [] if include_partial
        else [m for m in pivot.index if m in partial_months]
    )

    pivot = pivot.drop(index=months_to_drop)
    site_counts = site_counts.drop(index=months_to_drop)

    if pivot.empty:
        st.warning("No data left after removing partial months.")
        return

    min_date = pivot.index.min().date()
    max_date = pivot.index.max().date()

    date_range = st.sidebar.date_input(
        "Date range",
        value=(min_date, max_date),
        min_value=min_date,
        max_value=max_date,
        key="tt_dates"
    )

    if isinstance(date_range, (tuple, list)) and len(date_range) == 2:
        start_date, end_date = date_range
    else:
        start_date, end_date = min_date, max_date

    start_month = pd.Timestamp(start_date).to_period("M").to_timestamp()
    end_month = pd.Timestamp(end_date).to_period("M").to_timestamp()

    in_range = (pivot.index >= start_month) & (pivot.index <= end_month)

    pivot = pivot[in_range]
    site_counts = site_counts[in_range]

    st.sidebar.divider()
    st.sidebar.subheader("Top movers")

    max_window = max(1, len(pivot) // 2)

    window = st.sidebar.slider(
        "Compare last N months to the N before",
        min_value=1,
        max_value=min(12, max_window),
        value=min(3, max_window),
        key="tt_window",
        help=(
            "A single month-to-month change is noisy. Comparing "
            "several months at a time is more reliable."
        )
    )

    min_volume = st.sidebar.slider(
        "Minimum questions in the earlier period",
        min_value=1,
        max_value=200,
        value=20,
        key="tt_min_volume",
        help=(
            "Hides tiny tags, where going from 2 to 5 questions would "
            "look like +150%."
        )
    )

    PCT_OPTION = "% change"
    ABS_OPTION = "Absolute change (questions)"
    REL_OPTION = "% change relative to site"

    measure_options = [PCT_OPTION, ABS_OPTION]

    if has_site:
        measure_options.append(REL_OPTION)

    measure = st.sidebar.radio(
        "Rank tags by",
        options=measure_options,
        key="tt_measure",
        help=(
            "% change favors small tags that double. Absolute change "
            "(extra or fewer questions) favors big tags. Relative to "
            "site removes the effect of overall site activity rising "
            "or falling, so you see tags growing or shrinking faster "
            "than the site itself (only available when the data has "
            "'site' rows)."
        )
    )

    top_k = st.sidebar.slider(
        "Tags to show per direction",
        min_value=3,
        max_value=15,
        value=8,
        key="tt_top_k"
    )

    if len(pivot) < 2 * window:
        st.warning(
            f"Need at least {2 * window} months of data for a "
            f"{window}-month comparison, but only {len(pivot)} are "
            "available. Widen the date range or shorten the window."
        )
        return

    # ---- Compute top movers ----

    recent = pivot.iloc[-window:].sum()
    prior = pivot.iloc[-2 * window:-window].sum()

    movers = pd.DataFrame({"prior": prior, "recent": recent})

    movers["change"] = movers["recent"] - movers["prior"]

    movers = movers[movers["prior"] >= min_volume].copy()

    movers["pct_change"] = movers["change"] / movers["prior"]

    site_pct_change = None

    if has_site:

        site_recent = site_counts.iloc[-window:].sum()
        site_prior = site_counts.iloc[-2 * window:-window].sum()

        if site_prior > 0:

            site_pct_change = site_recent / site_prior - 1

            movers["vs_site"] = (
                (movers["recent"] / movers["prior"])
                / (site_recent / site_prior)
                - 1
            )

    use_relative = (
        measure == REL_OPTION
        and "vs_site" in movers.columns
    )

    use_absolute = measure == ABS_OPTION

    if use_relative:
        rank_col = "vs_site"
    elif use_absolute:
        rank_col = "change"
    else:
        rank_col = "pct_change"

    movers = movers.sort_values(rank_col, ascending=False)

    recent_window = pivot.index[-window:]
    prior_window = pivot.index[-2 * window:-window]

    # ---- Top movers display ----

    st.subheader("Top Movers")

    st.caption(
        f"Recent: {recent_window[0]:%b %Y} to {recent_window[-1]:%b %Y}. "
        f"Earlier: {prior_window[0]:%b %Y} to {prior_window[-1]:%b %Y}."
    )

    if movers.empty:

        st.warning(
            "No tags meet the minimum volume. Lower the minimum "
            "questions or widen the date range."
        )

        return

    col1, col2, col3 = st.columns(3)

    col1.metric("Tags compared", len(movers))

    col2.metric(
        "Growing tags",
        int((movers[rank_col] > 0).sum())
    )

    if site_pct_change is not None:
        col3.metric("Site-wide change", f"{site_pct_change:+.1%}")
    else:
        col3.metric("Shrinking tags", int((movers[rank_col] < 0).sum()))

    growers = movers[movers[rank_col] > 0].head(top_k)
    decliners = movers[movers[rank_col] < 0].tail(top_k)

    chart_df = pd.concat([growers, decliners]).reset_index(names="tag")

    chart_df["direction"] = np.where(
        chart_df[rank_col] > 0, "Growing", "Shrinking"
    )

    # Value drawn on the bars: questions for absolute change,
    # percent otherwise.
    if use_absolute:
        chart_df["rank_value"] = chart_df["change"]
    else:
        chart_df["rank_value"] = chart_df[rank_col] * 100

    chart_df["pct_display"] = chart_df["pct_change"] * 100

    if chart_df.empty:

        st.info("No tags are growing or shrinking under these filters.")

    else:

        if use_relative:
            axis_label = "% change vs. site"
        elif use_absolute:
            axis_label = "Change in number of questions"
        else:
            axis_label = "% change in questions"

        fig_movers = px.bar(
            chart_df,
            x="rank_value",
            y="tag",
            color="direction",
            orientation="h",
            color_discrete_map={
                "Growing": GROW_COLOR,
                "Shrinking": SHRINK_COLOR
            },
            hover_data={
                "prior": ":,.0f",
                "recent": ":,.0f",
                "change": ":+,.0f",
                "pct_display": ":+.1f",
                "rank_value": False,
                "direction": False,
                "tag": False
            },
            labels={
                "rank_value": axis_label,
                "pct_display": "% change",
                "tag": "Tag",
                "prior": "Earlier period",
                "recent": "Recent period",
                "change": "Change"
            },
            title="Fastest Growing and Shrinking Tags"
        )

        fig_movers.update_yaxes(
            categoryorder="array",
            categoryarray=chart_df["tag"].tolist(),
            autorange="reversed"
        )

        fig_movers.add_vline(x=0, line_color="gray", line_width=1)

        fig_movers.update_layout(
            template="plotly_white",
            legend_title="",
            height=max(400, 28 * len(chart_df) + 120)
        )

        st.plotly_chart(fig_movers, width="stretch")

    with st.expander("See all movers in a table"):

        table = movers.reset_index(names="Tag").rename(columns={
            "prior": "Earlier period",
            "recent": "Recent period",
            "change": "Change",
            "pct_change": "% change",
            "vs_site": "% change vs. site"
        })

        table["% change"] = (table["% change"] * 100).round(1)

        if "% change vs. site" in table.columns:
            table["% change vs. site"] = (
                table["% change vs. site"] * 100
            ).round(1)

        st.dataframe(table, width="stretch", hide_index=True)

    st.divider()

    # ---- Trend chart ----

    st.subheader("Monthly Trends")

    metric_options = [
        m for m in TREND_METRICS
        if has_site or m != "Share of site (%)"
    ]

    ctrl1, ctrl2 = st.columns(2)

    with ctrl1:
        trend_metric = st.selectbox(
            "Metric",
            options=metric_options,
            key="tt_trend_metric",
            help=(
                "Share of site and Indexed put big and small tags on a "
                "comparable scale."
            )
        )

    with ctrl2:
        smoothing = st.selectbox(
            "Smoothing",
            options=[1, 3],
            format_func=lambda n: "None" if n == 1 else f"{n}-month average",
            key="tt_smoothing"
        )

    # Default selection: the biggest growers and decliners.
    default_tags = (
        growers.head(3).index.tolist()
        + decliners.tail(3).index.tolist()
    )

    tag_options = (
        pivot.sum().sort_values(ascending=False).index.tolist()
    )

    selected_tags = st.multiselect(
        "Tags to plot (max 10):",
        options=tag_options,
        default=[t for t in default_tags if t in tag_options],
        max_selections=10,
        key="tt_selected_tags"
    )

    if not selected_tags:

        st.info("Please select at least one tag to display the chart.")

        return

    plot = pivot[selected_tags].copy()
    site_plot = site_counts.copy()

    # Smooth the tag and site series the same way so shares stay valid.
    if smoothing > 1:
        plot = plot.rolling(smoothing, min_periods=1).mean()
        site_plot = site_plot.rolling(smoothing, min_periods=1).mean()

    if trend_metric == "Share of site (%)":

        plot = plot.div(site_plot.replace(0, np.nan), axis=0) * 100
        y_title = "Share of all site questions (%)"

    elif trend_metric == "Indexed to 100 at start":

        base = plot.apply(
            lambda s: s[s > 0].iloc[0] if (s > 0).any() else np.nan
        )

        plot = plot.div(base) * 100
        y_title = "Index (first month with questions = 100)"

    elif trend_metric == "Month-over-month change (%)":

        plot = (
            plot.pct_change(fill_method=None)
            .replace([np.inf, -np.inf], np.nan)
            * 100
        )
        y_title = "Change from previous month (%)"

    else:

        y_title = "Number of Questions"

    long_df = (
        plot.rename_axis("month")
        .reset_index()
        .melt(id_vars="month", var_name="tag", value_name="value")
    )

    fig = px.line(
        long_df,
        x="month",
        y="value",
        color="tag",
        markers=True,
        title=f"Monthly Tag Trends: {trend_metric}"
    )

    if trend_metric == "Month-over-month change (%)":
        fig.add_hline(y=0, line_color="gray", line_width=1)

    if trend_metric == "Indexed to 100 at start":
        fig.add_hline(y=100, line_dash="dot", line_color="gray")

    fig.update_layout(
        hovermode="x unified",
        xaxis_title="Month",
        yaxis_title=y_title,
        legend_title="Tag",
        template="plotly_white"
    )

    fig.update_xaxes(tickformat="%b %Y")

    st.plotly_chart(fig, width="stretch")

    notes = []

    if months_to_drop:
        notes.append(
            "Partial months excluded: "
            + ", ".join(m.strftime("%b %Y") for m in months_to_drop)
        )

    if not has_site:
        notes.append(
            "No 'site' rows found, so share-of-site and "
            "relative-to-site views are unavailable."
        )

    if notes:
        st.caption(" ".join(notes))


# ============================================================
# PAGE 2: POST ACTIVITY
# ============================================================

ACTIVITY_METRICS = [
    "Questions (site-wide)",
    "Answers per question",
    "Questions & answers (pulled data)"
]

ACTIVITY_VIEWS = [
    "Monthly values",
    "Rolling average",
    "Year-over-year % change"
]

Y_AXIS_TITLES = {
    "Questions (site-wide)": "Questions per month",
    "Answers per question": "Answers per question",
    "Questions & answers (pulled data)": "Posts per month (pulled data)"
}

# Extra example events for this page only.
EXAMPLE_EVENTS = [
    {"date": "2020-03-11", "label": "WHO declares COVID-19 a pandemic", "color": "#4C78A8"},
    {"date": "2022-11-30", "label": "ChatGPT public launch", "color": "#E45756"},
    {"date": "2023-03-14", "label": "GPT-4 release", "color": "#F2A541"},
    {"date": "2023-03-23", "label": "ChatGPT plugins announced", "color": "#54A24B"},
    {"date": "2023-06-05", "label": "Moderator strike begins", "color": "#B279A2"},
    {"date": "2023-08-28", "label": "ChatGPT Enterprise launch", "color": "#8064A2"}
]

# A pulled-data month is hidden when fewer than this share of that month's
# site-wide questions are in the database (the pulled window has ragged edges).
COVERAGE_MIN = 0.8


def remove_custom_event(index):
    st.session_state.custom_events.pop(index)


def apply_view(wide, view, window):

    """
    Applies the chosen view to the full history first, so that
    narrowing the month range never removes the data needed for
    a rolling average or year-over-year comparison.
    """

    if view == "Rolling average":

        return wide.rolling(window, min_periods=window).mean()

    if view == "Year-over-year % change":

        return (
            (wide / wide.shift(12) - 1) * 100
        ).replace([np.inf, -np.inf], np.nan)

    return wide


def trailing_comparison(series, end_month, months=12):

    """
    Average of the last `months` months vs. the `months` before
    that, ending at end_month. Returns None if not available.
    """

    history = series.loc[:end_month]

    if len(history) < 2 * months:
        return None

    recent = history.iloc[-months:].mean()
    prior = history.iloc[-2 * months:-months].mean()

    if pd.isna(recent) or pd.isna(prior) or prior == 0:
        return None

    return recent, (recent / prior - 1) * 100


def add_event_marker(fig, date, label, color, position):

    """
    Vertical dashed line plus a label. Uses add_shape and
    add_annotation because add_vline with text can fail on
    datetime axes in some Plotly versions.
    """

    x = pd.Timestamp(date).strftime("%Y-%m-%d")

    fig.add_shape(
        type="line",
        xref="x",
        yref="paper",
        x0=x,
        x1=x,
        y0=0,
        y1=1,
        line=dict(color=color, width=2, dash="dash")
    )

    # Alternate sides so nearby labels do not overlap.
    fig.add_annotation(
        x=x,
        y=1,
        yref="paper",
        text=label,
        showarrow=False,
        textangle=-90,
        xanchor="right" if position % 2 == 0 else "left",
        yanchor="top",
        font=dict(size=10, color=color)
    )


def render_post_activity():

    st.header("How has overall site activity changed over time?")

    st.write(
        "Explore how question and answer activity on the site "
        "has changed month by month."
    )

    if "custom_events" not in st.session_state:
        st.session_state.custom_events = []

    # ---- Data: site-wide monthly counts + monthly aggregates of pulled rows ----

    site_df = run_query(
        """
        SELECT month, question_count, is_partial_month
        FROM public.tag_monthly_counts
        WHERE scope = 'site'
        ORDER BY month;
        """
    )

    if site_df.empty:
        st.info("No site-wide monthly counts found. Run `fill` to pull them.")
        return

    site_df["month"] = pd.to_datetime(site_df["month"])
    site_df["is_partial_month"] = site_df["is_partial_month"].astype(bool)

    site = site_df.set_index("month").sort_index()

    partial_months = site.index[site["is_partial_month"]]
    complete_site = site[~site["is_partial_month"]]

    if len(complete_site) < 2:

        st.error("Not enough complete months of data to display.")
        return

    # Continuous month index so gaps stay gaps (needed for the
    # rolling average and year-over-year calculations).
    full_index = pd.date_range(
        complete_site.index.min(),
        complete_site.index.max(),
        freq="MS"
    )

    def finalize(series):

        """Drops incomplete months and aligns to the full month index."""

        return (
            series.drop(index=partial_months, errors="ignore")
            .reindex(full_index)
        )

    q_monthly = run_query(
        """
        SELECT
            date_trunc('month', creation_date AT TIME ZONE 'UTC')::date AS month,
            COUNT(*) AS questions,
            COALESCE(SUM(answer_count), 0) AS answer_sum
        FROM public.questions
        GROUP BY 1;
        """
    )

    a_monthly = run_query(
        """
        SELECT
            date_trunc('month', creation_date AT TIME ZONE 'UTC')::date AS month,
            COUNT(*) AS answers
        FROM public.answers
        GROUP BY 1;
        """
    )

    q_monthly["month"] = pd.to_datetime(q_monthly["month"])
    a_monthly["month"] = pd.to_datetime(a_monthly["month"])

    q_monthly = q_monthly.set_index("month").sort_index()
    a_monthly = a_monthly.set_index("month").sort_index()

    questions_raw = q_monthly["questions"].astype(float)

    site_questions = finalize(complete_site["question_count"])

    pulled_questions = finalize(questions_raw)
    pulled_answers = finalize(a_monthly["answers"].astype(float))

    # Uses the question's own answer_count, which stays accurate even
    # when not every answer row was pulled.
    answers_per_question = finalize(
        q_monthly["answer_sum"].astype(float) / questions_raw
    )

    # ---- Sidebar controls ----

    st.sidebar.header("Filters")

    month_options = list(full_index)

    start_month, end_month = st.sidebar.select_slider(
        "Month range",
        options=month_options,
        value=(month_options[0], month_options[-1]),
        format_func=lambda m: m.strftime("%b %Y"),
        key="pa_months",
        help="Whole months only, so partial months never distort the trend."
    )

    st.sidebar.divider()
    st.sidebar.subheader("Display")

    metric = st.sidebar.selectbox(
        "Metric",
        options=ACTIVITY_METRICS,
        key="pa_metric",
        help=(
            "Site-wide questions come from the monthly site totals. "
            "The other two are based on the questions and answers "
            "that were pulled, which may be a subset of the site."
        )
    )

    view = st.sidebar.selectbox(
        "View",
        options=ACTIVITY_VIEWS,
        key="pa_view",
        help=(
            "Rolling averages smooth out seasonality and noise. "
            "Year-over-year compares each month with the same month "
            "a year earlier."
        )
    )

    window = 12

    if view == "Rolling average":

        window = st.sidebar.selectbox(
            "Rolling window (months)",
            options=[3, 6, 12],
            index=1,
            key="pa_window"
        )

    chart_type = st.sidebar.radio(
        "Chart type",
        options=["Line", "Bar"],
        horizontal=True,
        key="pa_chart_type"
    )

    hide_incomplete = st.sidebar.checkbox(
        "Hide months with incomplete pulled data",
        value=True,
        key="pa_hide_incomplete",
        help=(
            "The pulled questions and answers only cover part of the "
            "site's history. Months where less than "
            f"{COVERAGE_MIN:.0%} of that month's site-wide questions "
            "are in the database are hidden from the two pulled-data "
            "metrics."
        )
    )

    if hide_incomplete:

        incomplete = (pulled_questions / site_questions) < COVERAGE_MIN

        pulled_questions = pulled_questions.mask(incomplete)
        pulled_answers = pulled_answers.mask(incomplete)
        answers_per_question = answers_per_question.mask(incomplete)

    SERIES = {
        "Questions (site-wide)": pd.DataFrame(
            {"Questions": site_questions}
        ),
        "Answers per question": pd.DataFrame(
            {"Answers per question": answers_per_question}
        ),
        "Questions & answers (pulled data)": pd.DataFrame(
            {"Questions": pulled_questions, "Answers": pulled_answers}
        )
    }

    st.sidebar.divider()
    st.sidebar.subheader("Events")

    show_events = st.sidebar.checkbox(
        "Show event markers", value=True, key="pa_show_events"
    )

    example_labels = [event["label"] for event in EXAMPLE_EVENTS]

    selected_examples = st.sidebar.multiselect(
        "Example events",
        options=example_labels,
        default=example_labels,
        key="pa_examples",
        help="Placeholder examples. Markers show timing, not cause."
    )

    with st.sidebar.expander("Add your own event"):

        with st.form("add_event_form", clear_on_submit=True):

            new_date = st.date_input(
                "Date",
                value=pd.Timestamp.now().date(),
                min_value=pd.Timestamp("2008-01-01").date(),
                key="pa_new_date"
            )

            new_label = st.text_input("Label", key="pa_new_label")

            new_color = st.color_picker(
                "Color", value="#1F77B4", key="pa_new_color"
            )

            submitted = st.form_submit_button("Add event")

        if submitted:

            if new_label.strip():

                st.session_state.custom_events.append({
                    "date": pd.Timestamp(new_date).strftime("%Y-%m-%d"),
                    "label": new_label.strip(),
                    "color": new_color
                })

            else:

                st.warning("Please enter a label for the event.")

        if st.session_state.custom_events:

            st.caption("Your events (this session only)")

            for i, event in enumerate(st.session_state.custom_events):

                label_col, button_col = st.columns([3, 1])

                label_col.caption(f"{event['date']}: {event['label']}")

                button_col.button(
                    "Remove",
                    key=f"remove_event_{i}",
                    on_click=remove_custom_event,
                    args=(i,)
                )

    # ---- Summary numbers ----

    st.subheader("Summary")

    in_range = site_questions.loc[start_month:end_month].dropna()

    if in_range.empty:

        st.info("No complete months in the selected range.")

    else:

        peak_month = in_range.idxmax()
        peak_value = in_range.max()
        latest_month = in_range.index[-1]
        latest_value = in_range.iloc[-1]

        vs_peak = (latest_value / peak_value - 1) * 100

        question_trend = trailing_comparison(site_questions, end_month)
        apq_trend = trailing_comparison(answers_per_question, end_month)

        col1, col2, col3, col4 = st.columns(4)

        col1.metric(
            f"Peak month ({peak_month:%b %Y})",
            f"{peak_value:,.0f}",
            help="Highest monthly question count in the selected range."
        )

        col2.metric(
            f"Latest month ({latest_month:%b %Y})",
            f"{latest_value:,.0f}",
            f"{vs_peak:+.0f}% vs. peak",
            delta_color="off"
        )

        if question_trend:

            col3.metric(
                "Questions per month, last 12 months",
                f"{question_trend[0]:,.0f}",
                f"{question_trend[1]:+.1f}% vs. prior 12 months",
                delta_color="off"
            )

        else:

            col3.metric(
                "Questions per month, last 12 months",
                "n/a",
                help="Needs 24 complete months ending at the selected end month."
            )

        if apq_trend:

            col4.metric(
                "Answers per question, last 12 months",
                f"{apq_trend[0]:.2f}",
                f"{apq_trend[1]:+.1f}% vs. prior 12 months",
                delta_color="off"
            )

        else:

            col4.metric(
                "Answers per question, last 12 months",
                "n/a",
                help=(
                    "Needs 24 complete months of pulled data ending at "
                    "the selected end month."
                )
            )

        st.caption(
            "Summary figures use complete months only and end at "
            f"{end_month:%b %Y}."
        )

    st.divider()

    # ---- Chart ----

    plot_wide = apply_view(
        SERIES[metric], view, window
    ).loc[start_month:end_month]

    plot_long = (
        plot_wide.rename_axis("Month")
        .reset_index()
        .melt(id_vars="Month", var_name="Series", value_name="Value")
    )

    # Collect events to display.
    all_events = [
        event for event in EXAMPLE_EVENTS
        if event["label"] in selected_examples
    ] + st.session_state.custom_events

    range_end = end_month + pd.offsets.MonthBegin(1)

    visible_events = sorted(
        [
            event for event in all_events
            if start_month <= pd.Timestamp(event["date"]) < range_end
        ],
        key=lambda event: event["date"]
    ) if show_events else []

    chart_col, event_col = st.columns([4, 1])

    with chart_col:

        if not plot_long["Value"].notna().any():

            st.info(
                "Not enough data for this view in the selected range. "
                "Try widening the month range, or untick 'Hide months "
                "with incomplete pulled data'."
            )

        else:

            if view == "Rolling average":
                title = f"{metric}: {window}-month rolling average"
            elif view == "Year-over-year % change":
                title = f"{metric}: change vs. same month last year"
            else:
                title = metric

            if chart_type == "Line":

                fig = px.line(
                    plot_long,
                    x="Month",
                    y="Value",
                    color="Series",
                    markers=True,
                    title=title
                )

            else:

                fig = px.bar(
                    plot_long,
                    x="Month",
                    y="Value",
                    color="Series",
                    barmode="group" if view == "Year-over-year % change" else "stack",
                    title=title
                )

            for position, event in enumerate(visible_events):

                add_event_marker(
                    fig,
                    event["date"],
                    event["label"],
                    event["color"],
                    position
                )

            if view == "Year-over-year % change":

                fig.add_hline(y=0, line_width=1, line_color="gray")
                fig.update_yaxes(ticksuffix="%")
                y_title = "Change vs. same month last year (%)"

            else:

                # Start at zero so declines are not exaggerated.
                fig.update_yaxes(rangemode="tozero")
                y_title = Y_AXIS_TITLES[metric]

            fig.update_layout(
                hovermode="x unified",
                xaxis_title="Month",
                yaxis_title=y_title,
                legend_title="Series",
                template="plotly_white"
            )

            fig.update_xaxes(tickformat="%b %Y")

            st.plotly_chart(fig, width="stretch")

        if len(partial_months) > 0:

            st.caption(
                "Incomplete month excluded: "
                + ", ".join(m.strftime("%b %Y") for m in partial_months)
                + "."
            )

        if metric != "Questions (site-wide)":

            st.caption(
                "Based on the questions and answers that were pulled, "
                "which may be a subset of the whole site."
            )

    # ---- Event legend ----

    with event_col:

        st.markdown("### Important Events")

        if visible_events:

            for event in visible_events:

                formatted_date = pd.Timestamp(event["date"]).strftime("%b %d, %Y")

                st.markdown(
                    f"""
                    <span style="
                        display: inline-block;
                        width: 11px;
                        height: 11px;
                        border-radius: 50%;
                        background-color: {event['color']};
                        margin-right: 7px;
                    "></span>
                    **{formatted_date}**
                    """,
                    unsafe_allow_html=True
                )

                st.caption(event["label"])

                st.divider()

        else:

            st.caption("No events to show in this month range.")


# ============================================================
# PAGE 3: TAG RELATIONSHIPS
# ============================================================

# Upper bound of the "Maximum tags" slider. Beyond this the
# network and heatmap become unreadable.
MAX_TAGS_CAP = 100

# Edge strength metrics: label -> column in the pair table.
REL_METRICS = {
    "Raw count": "co_occurrences",
    "Jaccard similarity": "jaccard",
    "Lift": "lift"
}

# Number of line-width groups used when drawing network edges.
EDGE_WIDTH_BUCKETS = 5

# Color for nodes that do not belong to a multi-tag community.
NEUTRAL_NODE_COLOR = "#B0B0B0"

PAIR_COLUMNS = [
    "tag_1", "tag_2", "co_occurrences",
    "jaccard", "lift", "p_2_given_1", "p_1_given_2"
]


def format_metric(value, metric_col):

    """Formats a metric value for hover text."""

    if metric_col == "co_occurrences":
        return f"{value:,.0f}"

    return f"{value:.3f}"


@st.cache_data(ttl=600)
def compute_relationships(start_date, end_date):

    """
    Tag frequencies and tag-pair statistics for questions created in the
    date range. Counting is done in SQL.

    Returns:
        tag_counts: questions per tag (sorted, descending)
        pair_df: one row per tag pair with
            co_occurrences  questions that have both tags
            jaccard         both / (either), from 0 to 1
            lift            observed / expected co-occurrence if the tags
                            were independent (1 = no association, >1 =
                            together more than chance)
            p_2_given_1     share of tag_1 questions also tagged tag_2
            p_1_given_2     share of tag_2 questions also tagged tag_1
        total: number of tagged questions in the range
    """

    start_dt, end_dt = utc_range(start_date, end_date)

    params = {"start": start_dt, "end": end_dt}

    counts = _read(
        """
        SELECT qt.tag_name AS tag, COUNT(*) AS posts
        FROM public.question_tags qt
        JOIN public.questions q ON q.question_id = qt.question_id
        WHERE q.creation_date >= :start AND q.creation_date < :end
        GROUP BY qt.tag_name
        ORDER BY posts DESC, qt.tag_name;
        """,
        params
    )

    tag_counts = counts.set_index("tag")["posts"]

    total = int(_read(
        """
        SELECT COUNT(DISTINCT qt.question_id) AS n
        FROM public.question_tags qt
        JOIN public.questions q ON q.question_id = qt.question_id
        WHERE q.creation_date >= :start AND q.creation_date < :end;
        """,
        params
    ).loc[0, "n"])

    # Each tag pair is counted once (tag_1 < tag_2).
    pair_df = _read(
        """
        SELECT
            a.tag_name AS tag_1,
            b.tag_name AS tag_2,
            COUNT(*) AS co_occurrences
        FROM public.question_tags a
        JOIN public.question_tags b
          ON a.question_id = b.question_id
         AND a.tag_name < b.tag_name
        JOIN public.questions q ON q.question_id = a.question_id
        WHERE q.creation_date >= :start AND q.creation_date < :end
        GROUP BY a.tag_name, b.tag_name;
        """,
        params
    )

    if pair_df.empty:

        return tag_counts, pd.DataFrame(columns=PAIR_COLUMNS), total

    count_1 = pair_df["tag_1"].map(tag_counts)
    count_2 = pair_df["tag_2"].map(tag_counts)
    both = pair_df["co_occurrences"]

    pair_df["jaccard"] = both / (count_1 + count_2 - both)
    pair_df["lift"] = both * total / (count_1 * count_2)
    pair_df["p_2_given_1"] = both / count_1
    pair_df["p_1_given_2"] = both / count_2

    return tag_counts, pair_df[PAIR_COLUMNS], total


def select_tags(
    tag_counts,
    pair_df,
    max_tags,
    min_tag_posts,
    min_cooccurrences,
    focus_tags,
    neighbors_per_tag,
    metric_col
):

    """
    Chooses which tags to display.

    No focus tags: the most popular tags (at least
    min_tag_posts questions each), up to max_tags.

    Focus tags: the focus tags themselves plus each one's
    strongest neighbors, ranked by the selected metric.
    """

    eligible = tag_counts[tag_counts >= min_tag_posts]

    if not focus_tags:

        return eligible.head(max_tags).index.tolist()

    # Focus tags missing from the date range are skipped.
    selected = [
        tag for tag in focus_tags
        if tag in tag_counts.index
    ]

    eligible_tags = set(eligible.index)

    if not pair_df.empty:

        strong_pairs = pair_df[
            pair_df["co_occurrences"] >= min_cooccurrences
        ]

        for tag in list(selected):

            related = strong_pairs[
                (strong_pairs["tag_1"] == tag)
                | (strong_pairs["tag_2"] == tag)
            ].copy()

            related["neighbor"] = related["tag_1"].where(
                related["tag_2"] == tag,
                related["tag_2"]
            )

            related = related[
                related["neighbor"].isin(eligible_tags)
            ]

            neighbors = (
                related.sort_values(metric_col, ascending=False)
                .head(neighbors_per_tag)["neighbor"]
            )

            selected.extend(neighbors)

    # Remove duplicates while preserving order.
    return list(dict.fromkeys(selected))


def filter_relationships(selected_tags, pair_df, min_cooccurrences):

    # Only retain pairs where both tags are selected and the
    # pair appears together often enough.
    return pair_df[
        pair_df["tag_1"].isin(selected_tags)
        & pair_df["tag_2"].isin(selected_tags)
        & (pair_df["co_occurrences"] >= min_cooccurrences)
    ].copy()


def calculate_layout_spacing(
    num_nodes,
    num_edges,
    min_cooccurrences,
    max_threshold
):

    """
    Returns the spring-layout "k" value (ideal distance between
    nodes).

    - threshold_factor: grows with the minimum co-occurrence threshold,
      because fewer edges leave clusters loosely held and drifting
      together.
    - density_factor: grows with graph density. Many nodes plus a
      low threshold gives a dense graph whose attractive forces
      collapse it into a blob.
    """

    base_k = 2.0 / math.sqrt(max(num_nodes, 1))

    threshold_ratio = (
        (min_cooccurrences - 1) / max(max_threshold - 1, 1)
    )

    threshold_factor = 1.0 + 1.5 * threshold_ratio

    max_edges = num_nodes * (num_nodes - 1) / 2

    density = num_edges / max_edges if max_edges > 0 else 0.0

    density_factor = 1.0 + 3.0 * density

    return base_k * threshold_factor * density_factor


def separate_overlapping_nodes(positions, min_distance, iterations=100):

    """
    Pushes apart any nodes closer than min_distance, so every
    pair of nodes keeps a minimum gap.
    """

    nodes = list(positions.keys())

    if len(nodes) < 2:
        return positions

    rng = np.random.default_rng(42)

    coords = np.array(
        [positions[node] for node in nodes],
        dtype=float
    )

    # Tiny jitter so identical points can be separated.
    coords += rng.normal(0, 1e-6, coords.shape)

    for _ in range(iterations):

        delta = coords[:, None, :] - coords[None, :, :]

        dist = np.linalg.norm(delta, axis=2)

        np.fill_diagonal(dist, np.inf)

        too_close = dist < min_distance

        if not too_close.any():
            break

        push = np.where(
            too_close,
            (min_distance - dist) / 2,
            0.0
        )

        direction = delta / np.where(
            too_close,
            dist,
            1.0
        )[:, :, None]

        coords += (direction * push[:, :, None]).sum(axis=1)

    return {
        node: coords[i]
        for i, node in enumerate(nodes)
    }


def detect_communities(G):

    """
    Returns {tag: community index}. Communities are numbered by
    size (0 = largest). Tags that are alone in their community
    get -1 so they can be drawn in a neutral color.
    """

    if G.number_of_edges() == 0:
        return {node: -1 for node in G.nodes}

    try:

        communities = nx.community.louvain_communities(
            G,
            weight="layout_weight",
            seed=42
        )

    except AttributeError:

        communities = nx.community.greedy_modularity_communities(
            G,
            weight="layout_weight"
        )

    communities = sorted(communities, key=len, reverse=True)

    membership = {}

    group = 0

    for members in communities:

        if len(members) < 2:

            for node in members:
                membership[node] = -1

        else:

            for node in members:
                membership[node] = group

            group += 1

    return membership


def create_network_graph(
    selected_tags,
    filtered_pairs,
    tag_counts,
    min_cooccurrences,
    max_threshold,
    metric_col,
    metric_label,
    color_by_community
):

    G = nx.Graph()

    # Edge weights are normalized to 0-1 for the layout only.
    # Raw values (counts can be in the hundreds) make the spring
    # layout pull strongly connected tags into one tight clump.
    max_pair_weight = max(
        filtered_pairs[metric_col].max()
        if not filtered_pairs.empty
        else 1,
        1e-9
    )

    for tag in selected_tags:

        G.add_node(tag, frequency=int(tag_counts[tag]))

    for row in filtered_pairs.itertuples(index=False):

        weight = float(getattr(row, metric_col))

        G.add_edge(
            row.tag_1,
            row.tag_2,
            weight=weight,
            posts=int(row.co_occurrences),
            layout_weight=weight / max_pair_weight
        )

    # Calculate node positions.
    if G.number_of_edges() > 0:

        spacing = calculate_layout_spacing(
            G.number_of_nodes(),
            G.number_of_edges(),
            min_cooccurrences,
            max_threshold
        )

        positions = nx.spring_layout(
            G,
            seed=42,
            weight="layout_weight",
            k=spacing,
            iterations=200
        )

        positions = separate_overlapping_nodes(
            positions,
            min_distance=1.4 / math.sqrt(G.number_of_nodes())
        )

    else:

        positions = nx.circular_layout(G)

    fig = go.Figure()

    max_edge_weight = max(
        [data["weight"] for _, _, data in G.edges(data=True)],
        default=1
    )

    max_edge_weight = max(max_edge_weight, 1e-9)

    # Edges: one line trace per width bucket (None separators
    # break the line between edges), instead of one per edge.
    bucket_x = [[] for _ in range(EDGE_WIDTH_BUCKETS)]
    bucket_y = [[] for _ in range(EDGE_WIDTH_BUCKETS)]

    mid_x = []
    mid_y = []
    mid_hover = []

    for source, target, data in G.edges(data=True):

        x0, y0 = positions[source]
        x1, y1 = positions[target]

        bucket = min(
            int(EDGE_WIDTH_BUCKETS * data["weight"] / max_edge_weight),
            EDGE_WIDTH_BUCKETS - 1
        )

        bucket_x[bucket].extend([x0, x1, None])
        bucket_y[bucket].extend([y0, y1, None])

        # Invisible marker at the edge midpoint carries the hover
        # text (a two-point line only hovers at its endpoints).
        mid_x.append((x0 + x1) / 2)
        mid_y.append((y0 + y1) / 2)

        mid_hover.append(
            f"{source} + {target}"
            f"<br>Questions together: {data['posts']:,}"
            f"<br>{metric_label}: "
            f"{format_metric(data['weight'], metric_col)}"
        )

    for bucket in range(EDGE_WIDTH_BUCKETS):

        if not bucket_x[bucket]:
            continue

        fig.add_trace(
            go.Scatter(
                x=bucket_x[bucket],
                y=bucket_y[bucket],
                mode="lines",
                line=dict(
                    width=1 + 5 * (bucket + 0.5) / EDGE_WIDTH_BUCKETS,
                    color="rgba(120,120,120,0.5)"
                ),
                hoverinfo="skip",
                showlegend=False
            )
        )

    if mid_x:

        fig.add_trace(
            go.Scatter(
                x=mid_x,
                y=mid_y,
                mode="markers",
                marker=dict(size=14, color="rgba(0,0,0,0)"),
                hovertext=mid_hover,
                hoverinfo="text",
                showlegend=False
            )
        )

    # Nodes
    membership = detect_communities(G) if color_by_community else {}

    palette = px.colors.qualitative.D3

    node_x = []
    node_y = []
    node_sizes = []
    node_colors = []
    node_hover = []

    max_frequency = max(
        [tag_counts[tag] for tag in selected_tags],
        default=1
    )

    for tag in selected_tags:

        x, y = positions[tag]

        frequency = int(tag_counts[tag])

        node_x.append(x)
        node_y.append(y)

        node_sizes.append(12 + 35 * frequency / max_frequency)

        hover = (
            f"Tag: {tag}"
            f"<br>Questions: {frequency:,}"
            f"<br>Connections: {G.degree(tag)}"
        )

        if color_by_community:

            group = membership.get(tag, -1)

            if group < 0:

                node_colors.append(NEUTRAL_NODE_COLOR)

            else:

                node_colors.append(palette[group % len(palette)])

                hover += f"<br>Group: {group + 1}"

        else:

            node_colors.append("#3498db")

        node_hover.append(hover)

    fig.add_trace(
        go.Scatter(
            x=node_x,
            y=node_y,
            mode="markers+text",
            text=list(selected_tags),
            textposition="top center",
            hovertext=node_hover,
            hoverinfo="text",
            marker=dict(
                size=node_sizes,
                color=node_colors,
                line=dict(width=1, color="white")
            ),
            showlegend=False
        )
    )

    fig.update_layout(
        title="Tag Co-occurrence Network",
        showlegend=False,
        hovermode="closest",
        height=650,
        margin=dict(l=20, r=20, t=50, b=20),
        xaxis=dict(showgrid=False, zeroline=False, showticklabels=False),
        yaxis=dict(showgrid=False, zeroline=False, showticklabels=False)
    )

    return fig


def order_by_similarity(tags, pair_df):

    """
    Orders tags by hierarchical clustering on Jaccard distance,
    so related tags sit next to each other and form visible
    blocks. Falls back to the given order if scipy is missing.
    """

    if len(tags) < 3:
        return list(tags)

    try:
        from scipy.cluster.hierarchy import leaves_list, linkage
        from scipy.spatial.distance import squareform
    except ImportError:
        return list(tags)

    index = {tag: i for i, tag in enumerate(tags)}

    similarity = np.zeros((len(tags), len(tags)))

    sub = pair_df[
        pair_df["tag_1"].isin(index) & pair_df["tag_2"].isin(index)
    ]

    rows = sub["tag_1"].map(index).to_numpy()
    cols = sub["tag_2"].map(index).to_numpy()

    similarity[rows, cols] = sub["jaccard"].to_numpy()
    similarity[cols, rows] = sub["jaccard"].to_numpy()

    distance = 1.0 - similarity

    np.fill_diagonal(distance, 0.0)

    order = leaves_list(
        linkage(squareform(distance, checks=False), method="average")
    )

    return [tags[i] for i in order]


def create_heatmap(
    ordered_tags,
    filtered_pairs,
    metric_col,
    metric_label
):

    index = {tag: i for i, tag in enumerate(ordered_tags)}

    # Start from NaN so pairs hidden by the threshold render
    # blank instead of looking like "never appear together".
    raw = np.full((len(ordered_tags), len(ordered_tags)), np.nan)

    rows = filtered_pairs["tag_1"].map(index).to_numpy()
    cols = filtered_pairs["tag_2"].map(index).to_numpy()

    values = filtered_pairs[metric_col].to_numpy(dtype=float)

    raw[rows, cols] = values
    raw[cols, rows] = values

    if metric_col == "lift":

        # 1 means no association, so use a diverging scale
        # centered on 1. Log scale keeps 0.5x and 2x symmetric.
        z = np.log2(raw)

        heatmap = go.Heatmap(
            z=z,
            x=list(ordered_tags),
            y=list(ordered_tags),
            customdata=raw,
            colorscale="RdBu",
            zmid=0,
            hovertemplate=(
                "%{y} + %{x}"
                f"<br>{metric_label}: "
                "%{customdata:.2f}<extra></extra>"
            ),
            colorbar=dict(
                title=metric_label,
                tickvals=[-2, -1, 0, 1, 2, 3, 4],
                ticktext=["0.25", "0.5", "1", "2", "4", "8", "16"]
            )
        )

    else:

        heatmap = go.Heatmap(
            z=raw,
            x=list(ordered_tags),
            y=list(ordered_tags),
            colorscale="Blues",
            hovertemplate=(
                "%{y} + %{x}"
                f"<br>{metric_label}: "
                + (
                    "%{z:,.0f}"
                    if metric_col == "co_occurrences"
                    else "%{z:.3f}"
                )
                + "<extra></extra>"
            ),
            colorbar=dict(title=metric_label)
        )

    fig = go.Figure(data=heatmap)

    fig.update_layout(
        title="Tag Co-occurrence Heatmap",
        height=700,
        xaxis_title="Tag",
        yaxis_title="Tag",
        xaxis=dict(tickangle=-45),
        yaxis=dict(autorange="reversed"),
        margin=dict(l=100, r=30, t=60, b=100)
    )

    return fig


# Label -> (column in the neighbor table, is a percentage).
EXPLORER_RANKINGS = {
    "% of this tag's questions": ("p_neighbor_given_tag", True),
    "Raw count": ("co_occurrences", False),
    "Jaccard similarity": ("jaccard", False),
    "Lift": ("lift", False)
}


def neighbors_of(tag, pair_df, tag_counts):

    """
    Returns one row per tag that appears with `tag`:
        neighbor, co_occurrences, jaccard, lift,
        p_neighbor_given_tag  share of `tag` questions that also
                              have the neighbor
        p_tag_given_neighbor  share of neighbor questions that
                              also have `tag`
        neighbor_questions    total questions with the neighbor
    """

    first = pair_df[pair_df["tag_1"] == tag].rename(columns={
        "tag_2": "neighbor",
        "p_2_given_1": "p_neighbor_given_tag",
        "p_1_given_2": "p_tag_given_neighbor"
    })

    second = pair_df[pair_df["tag_2"] == tag].rename(columns={
        "tag_1": "neighbor",
        "p_1_given_2": "p_neighbor_given_tag",
        "p_2_given_1": "p_tag_given_neighbor"
    })

    columns = [
        "neighbor",
        "co_occurrences",
        "jaccard",
        "lift",
        "p_neighbor_given_tag",
        "p_tag_given_neighbor"
    ]

    result = pd.concat(
        [first[columns], second[columns]],
        ignore_index=True
    )

    result["neighbor_questions"] = (
        result["neighbor"].map(tag_counts).astype(int)
    )

    return result


def create_neighbor_chart(tag, top, rank_label):

    rank_col, is_percent = EXPLORER_RANKINGS[rank_label]

    values = top[rank_col] * 100 if is_percent else top[rank_col]

    if is_percent:
        text_labels = [f"{v:.1f}%" for v in values]
    elif rank_col == "co_occurrences":
        text_labels = [f"{v:,.0f}" for v in values]
    else:
        text_labels = [f"{v:.2f}" for v in values]

    fig = go.Figure(
        go.Bar(
            x=values,
            y=top["neighbor"],
            orientation="h",
            marker_color="#4C78A8",
            text=text_labels,
            textposition="outside",
            customdata=np.column_stack([
                top["co_occurrences"],
                top["p_neighbor_given_tag"] * 100,
                top["p_tag_given_neighbor"] * 100,
                top["jaccard"],
                top["lift"],
                top["neighbor_questions"]
            ]),
            hovertemplate=(
                "<b>%{y}</b>"
                "<br>Questions together: %{customdata[0]:,.0f}"
                f"<br>% of {tag} questions with this tag: "
                "%{customdata[1]:.1f}%"
                f"<br>% of this tag's questions that also have {tag}: "
                "%{customdata[2]:.1f}%"
                "<br>Jaccard: %{customdata[3]:.3f}"
                "<br>Lift: %{customdata[4]:.2f}"
                "<br>Questions with this tag: %{customdata[5]:,.0f}"
                "<extra></extra>"
            )
        )
    )

    axis_title_text = (
        f"% of {tag} questions that also have this tag"
        if is_percent
        else rank_label
    )

    fig.update_layout(
        title=f"Tags that appear with {tag}",
        height=max(350, 30 * len(top) + 120),
        template="plotly_white",
        xaxis_title=axis_title_text,
        yaxis=dict(autorange="reversed"),
        margin=dict(l=20, r=60, t=60, b=40)
    )

    return fig


def render_tag_relationships():

    st.header("Which tags tend to appear together?")

    st.write(
        "Explore which Stack Exchange tags tend to "
        "appear together."
    )

    st.caption(
        "All numbers describe the questions in the loaded dataset. "
        "If those questions were collected by searching for specific "
        "tags, those tags will look more connected here than they "
        "would across the whole site (lift is most affected)."
    )

    min_date, max_date = get_question_dates()

    st.sidebar.header("Filters")

    date_range = st.sidebar.date_input(
        "Question creation date",
        value=(min_date, max_date),
        min_value=min_date,
        max_value=max_date,
        key="tr_dates"
    )

    start_date, end_date = read_date_range(date_range, min_date, max_date)

    tag_counts, pair_df, num_questions = compute_relationships(
        start_date,
        end_date
    )

    # Whole-dataset statistics. Used for stable control ranges and
    # options that do not shift when the date filter changes.
    all_tag_counts, full_pair_df, _ = compute_relationships(
        min_date,
        max_date
    )

    total_tags = len(all_tag_counts)

    # Scale the co-occurrence slider to the data: 95th percentile of
    # pair counts, so a few extreme pairs do not stretch the slider.
    if not full_pair_df.empty:

        max_cooccurrence_threshold = max(
            10,
            int(np.percentile(full_pair_df["co_occurrences"], 95))
        )

    else:

        max_cooccurrence_threshold = 10

    # ---- Sidebar controls ----

    st.sidebar.divider()

    st.sidebar.subheader("Tag selection")

    # Slider range comes from the whole dataset, so it does not
    # change (and silently reset the value) when the date changes.
    max_posts_per_tag = (
        int(all_tag_counts.max()) if total_tags > 0 else 1
    )

    if max_posts_per_tag > 1:

        min_tag_posts = st.sidebar.slider(
            "Minimum questions per tag",
            min_value=1,
            max_value=max_posts_per_tag,
            value=min(10, max_posts_per_tag),
            key="tr_min_tag_posts",
            help="Hides tags used in fewer questions than this."
        )

    else:

        min_tag_posts = 1

    focus_tags = st.sidebar.multiselect(
        "Focus on tags",
        options=all_tag_counts.index.tolist(),
        key="tr_focus_tags",
        help=(
            "Leave empty to show the most popular tags. "
            "Select tags to show them plus their strongest "
            "neighbors."
        )
    )

    if focus_tags:

        neighbors_per_tag = st.sidebar.slider(
            "Neighbors per focus tag",
            min_value=1,
            max_value=25,
            value=10,
            key="tr_neighbors"
        )

    else:

        neighbors_per_tag = 0

    tags_cap = min(MAX_TAGS_CAP, total_tags)

    if tags_cap > 5:

        max_tags = st.sidebar.slider(
            "Maximum tags",
            min_value=5,
            max_value=tags_cap,
            value=min(30, tags_cap),
            disabled=bool(focus_tags),
            key="tr_max_tags",
            help=(
                "Applies when no focus tags are selected. "
                "Disabled while focusing on specific tags."
            )
        )

    else:

        max_tags = tags_cap

    missing_focus_tags = [
        tag for tag in focus_tags
        if tag not in tag_counts.index
    ]

    if missing_focus_tags:

        st.sidebar.caption(
            "Not in the selected date range: "
            + ", ".join(missing_focus_tags)
        )

    st.sidebar.divider()

    st.sidebar.subheader("Relationships")

    min_cooccurrences = st.sidebar.slider(
        "Minimum co-occurrences",
        min_value=1,
        max_value=max_cooccurrence_threshold,
        value=min(10, max_cooccurrence_threshold),
        key="tr_min_co",
        help="Pairs seen together in fewer questions than this are hidden."
    )

    metric_label = st.sidebar.selectbox(
        "Relationship strength",
        options=list(REL_METRICS.keys()),
        key="tr_metric",
        help=(
            "Raw count favors popular tags. Jaccard similarity "
            "(0 to 1) and lift (1 = no association) highlight "
            "pairs that are tied together beyond popularity."
        )
    )

    metric_col = REL_METRICS[metric_label]

    st.sidebar.divider()

    st.sidebar.subheader("Display")

    color_by_community = st.sidebar.checkbox(
        "Color network by group",
        value=True,
        key="tr_community",
        help=(
            "Colors tags by detected community: sets of tags that "
            "co-occur mostly with each other (technology stacks)."
        )
    )

    hide_isolated = st.sidebar.checkbox(
        "Hide tags with no connections",
        value=True,
        key="tr_hide_isolated",
        help="Removes tags that have no pair above the threshold."
    )

    selected_tags = select_tags(
        tag_counts,
        pair_df,
        max_tags,
        min_tag_posts,
        min_cooccurrences,
        focus_tags,
        neighbors_per_tag,
        metric_col
    )

    filtered_pairs = filter_relationships(
        selected_tags,
        pair_df,
        min_cooccurrences
    )

    if hide_isolated:

        connected = set(filtered_pairs["tag_1"]) | set(filtered_pairs["tag_2"])

        selected_tags = [
            tag for tag in selected_tags if tag in connected
        ]

    # ---- Display ----

    col1, col2, col3 = st.columns(3)

    col1.metric("Questions", f"{num_questions:,}")
    col2.metric("Tags displayed", len(selected_tags))
    col3.metric("Connections", len(filtered_pairs))

    st.divider()

    tab_network, tab_heatmap, tab_explorer = st.tabs([
        "Network",
        "Heatmap",
        "Single tag explorer"
    ])

    no_tags_message = (
        "No tags to display. Try lowering the minimum co-occurrences "
        "or minimum questions per tag, or widening the date range."
    )

    with tab_network:

        if not selected_tags:

            st.warning(no_tags_message)

        else:

            st.caption(
                "Larger nodes represent more popular tags. "
                f"Thicker edges represent a higher {metric_label.lower()} "
                "between the two tags."
                + (
                    " Colors mark groups of tags that mostly appear "
                    "with each other; gray tags belong to no group."
                    if color_by_community
                    else ""
                )
            )

            st.plotly_chart(
                create_network_graph(
                    selected_tags,
                    filtered_pairs,
                    tag_counts,
                    min_cooccurrences,
                    max_cooccurrence_threshold,
                    metric_col,
                    metric_label,
                    color_by_community
                ),
                width="stretch"
            )

            with st.expander("Show pair data"):

                pair_table = (
                    filtered_pairs
                    .sort_values(metric_col, ascending=False)
                    .copy()
                )

                pair_table["p_2_given_1"] *= 100
                pair_table["p_1_given_2"] *= 100

                pair_table = pair_table.rename(columns={
                    "tag_1": "Tag 1",
                    "tag_2": "Tag 2",
                    "co_occurrences": "Questions together",
                    "jaccard": "Jaccard",
                    "lift": "Lift",
                    "p_2_given_1": "% of Tag 1 with Tag 2",
                    "p_1_given_2": "% of Tag 2 with Tag 1"
                })

                st.dataframe(
                    pair_table,
                    width="stretch",
                    hide_index=True,
                    column_config={
                        "Jaccard": st.column_config.NumberColumn(format="%.3f"),
                        "Lift": st.column_config.NumberColumn(format="%.2f"),
                        "% of Tag 1 with Tag 2": st.column_config.NumberColumn(
                            format="%.1f%%"
                        ),
                        "% of Tag 2 with Tag 1": st.column_config.NumberColumn(
                            format="%.1f%%"
                        )
                    }
                )

                st.download_button(
                    "Download pairs as CSV",
                    pair_table.to_csv(index=False),
                    file_name="tag_pairs.csv",
                    mime="text/csv",
                    key="tr_download_pairs"
                )

    with tab_heatmap:

        if not selected_tags:

            st.warning(no_tags_message)

        else:

            st.caption(
                f"Each cell shows the {metric_label.lower()} between "
                "two tags. Tags are ordered by similarity, so related "
                "tags form blocks. Blank cells are pairs below the "
                "minimum co-occurrence threshold."
                + (
                    " Blue means the tags appear together more than "
                    "chance, red less."
                    if metric_col == "lift"
                    else ""
                )
            )

            st.plotly_chart(
                create_heatmap(
                    order_by_similarity(selected_tags, pair_df),
                    filtered_pairs,
                    metric_col,
                    metric_label
                ),
                width="stretch"
            )

    with tab_explorer:

        st.caption(
            "Pick a tag to see which other tags appear with it most "
            "often. This tab uses the date range in the sidebar but "
            "ignores the tag selection and threshold controls."
        )

        if total_tags == 0:

            st.info("No tags available.")

        else:

            ctrl1, ctrl2, ctrl3, ctrl4 = st.columns(4)

            explorer_tag = ctrl1.selectbox(
                "Tag",
                options=all_tag_counts.index.tolist(),
                key="tr_explorer_tag",
                help="Type to search."
            )

            explorer_rank = ctrl2.selectbox(
                "Rank by",
                options=list(EXPLORER_RANKINGS.keys()),
                key="tr_explorer_rank",
                help=(
                    "The first option answers 'what share of this "
                    "tag's questions also carry each other tag?'. "
                    "Raw count favors popular tags; Jaccard and lift "
                    "favor tags that are tied to this one beyond "
                    "popularity."
                )
            )

            explorer_top_n = ctrl3.slider(
                "Number of tags",
                min_value=5,
                max_value=30,
                value=15,
                key="tr_explorer_top_n"
            )

            explorer_min = ctrl4.slider(
                "Minimum questions together",
                min_value=1,
                max_value=50,
                value=3,
                key="tr_explorer_min",
                help=(
                    "Hides tags seen with this one only a handful of "
                    "times. Matters most for lift, which is noisy for "
                    "rare tags."
                )
            )

            if explorer_tag not in tag_counts.index:

                st.info(
                    f"'{explorer_tag}' has no questions in the "
                    "selected date range."
                )

            else:

                neighbors = neighbors_of(explorer_tag, pair_df, tag_counts)

                neighbors = neighbors[
                    neighbors["co_occurrences"] >= explorer_min
                ]

                m1, m2 = st.columns(2)

                m1.metric(
                    f"Questions tagged {explorer_tag}",
                    f"{int(tag_counts[explorer_tag]):,}"
                )

                m2.metric(
                    "Tags it appears with",
                    f"{len(neighbors):,}"
                )

                if neighbors.empty:

                    st.info(
                        "No tags appear with this one often enough. "
                        "Try lowering the minimum questions together."
                    )

                else:

                    rank_col, _ = EXPLORER_RANKINGS[explorer_rank]

                    top = (
                        neighbors
                        .sort_values(rank_col, ascending=False)
                        .head(explorer_top_n)
                        .reset_index(drop=True)
                    )

                    st.plotly_chart(
                        create_neighbor_chart(
                            explorer_tag, top, explorer_rank
                        ),
                        width="stretch"
                    )

                    with st.expander("Show data"):

                        table = top.copy()

                        table["p_neighbor_given_tag"] *= 100
                        table["p_tag_given_neighbor"] *= 100

                        table = table[[
                            "neighbor",
                            "co_occurrences",
                            "p_neighbor_given_tag",
                            "p_tag_given_neighbor",
                            "jaccard",
                            "lift",
                            "neighbor_questions"
                        ]].rename(columns={
                            "neighbor": "Tag",
                            "co_occurrences": "Questions together",
                            "p_neighbor_given_tag": (
                                f"% of {explorer_tag} questions with this tag"
                            ),
                            "p_tag_given_neighbor": (
                                f"% of this tag's questions with {explorer_tag}"
                            ),
                            "jaccard": "Jaccard",
                            "lift": "Lift",
                            "neighbor_questions": "Questions with this tag"
                        })

                        st.dataframe(
                            table.round(3),
                            width="stretch",
                            hide_index=True
                        )

                        st.download_button(
                            "Download as CSV",
                            table.to_csv(index=False),
                            file_name=f"tags_with_{explorer_tag}.csv",
                            mime="text/csv",
                            key="tr_download_neighbors"
                        )


# ============================================================
# PAGE 4: TAG ATTENTION
# ============================================================

# Metric label -> column in the questions table.
ATT_METRICS = {
    "Score": "score",
    "Comments": "comment_count",
    "Views": "view_count"
}

# The three metrics plus the combined score (see compute_tag_stats).
ATT_ALL_METRICS = {**ATT_METRICS, "Combined": "combined"}

# Aggregation label -> pandas aggregation name.
ATT_AGGREGATIONS = {
    "Average per question": "mean",
    "Median per question": "median",
    "Total": "sum"
}

# Age adjustment label -> whether to convert to a per-30-day rate.
AGE_OPTIONS = {
    "Raw (no adjustment)": False,
    "Per 30 days of question age": True
}

# Questions younger than this are treated as this old when
# computing rates, so brand-new questions don't explode the
# per-day numbers.
MIN_AGE_DAYS = 30

# Distinct color per metric so panels are easy to tell apart.
ATT_COLORS = {
    "Score": "#54A24B",
    "Comments": "#F2A541",
    "Views": "#4C78A8",
    "Combined": "#B279A2"
}

# What each metric counts, used in axis titles and hover text.
METRIC_UNITS = {
    "score": "net votes",
    "comment_count": "comments",
    "view_count": "views"
}


def compute_tag_stats(data, agg, min_questions):

    """
    One row per tag: question_count, the three metrics, and a
    combined score (mean of the three percentile ranks, 0-100),
    plus a rank column (1 = highest) for each of them.
    """

    stats = (
        data.groupby("tag_name")
        .agg(
            question_count=("question_id", "nunique"),
            score=("score", agg),
            comment_count=("comment_count", agg),
            view_count=("view_count", agg)
        )
        .reset_index()
    )

    stats = stats[stats["question_count"] >= min_questions].copy()

    metric_cols = list(ATT_METRICS.values())

    stats["combined"] = (
        stats[metric_cols].rank(pct=True).mean(axis=1) * 100
    )

    for col in ATT_ALL_METRICS.values():

        stats[f"{col}_rank"] = (
            stats[col].rank(ascending=False, method="min")
            .astype(int)
        )

    return stats


def bootstrap_interval(values, agg, n_boot=200, seed=0):

    """95% bootstrap interval for a tag's mean or median."""

    n = len(values)

    if n < 2:
        return np.nan, np.nan

    # Keep the resampling matrix to ~2M numbers for huge tags.
    n_boot = int(min(n_boot, max(20, 2_000_000 // n)))

    rng = np.random.default_rng(seed)

    samples = values[rng.integers(0, n, size=(n_boot, n))]

    estimates = (
        samples.mean(axis=1)
        if agg == "mean"
        else np.median(samples, axis=1)
    )

    low, high = np.percentile(estimates, [2.5, 97.5])

    return low, high


def compute_intervals(data, top_stats, agg):

    """
    {metric column: (low array, high array)} for the displayed
    tags. Totals are exact counts, so they get no interval.
    """

    if agg == "sum":
        return {}

    groups = {
        tag: group
        for tag, group in data[
            data["tag_name"].isin(top_stats["tag_name"])
        ].groupby("tag_name")
    }

    intervals = {}

    for col in ATT_METRICS.values():

        lows, highs = [], []

        for tag in top_stats["tag_name"]:

            low, high = bootstrap_interval(
                groups[tag][col].to_numpy(dtype=float),
                agg
            )

            lows.append(low)
            highs.append(high)

        intervals[col] = (np.array(lows), np.array(highs))

    return intervals


def measure_text(col, agg, adjust_age):

    """Plain-language description, e.g. 'average views per question'."""

    if col == "combined":
        return "combined score (0-100)"

    unit = METRIC_UNITS[col]

    if agg == "sum":
        description = f"total {unit}"
    else:
        word = "average" if agg == "mean" else "median"
        description = f"{word} {unit} per question"

    if adjust_age:
        description += " per 30 days"

    return description


def capitalize_first(value):

    return value[0].upper() + value[1:]


def axis_title(col, agg, adjust_age):

    """Axis title with units, split over two lines."""

    return capitalize_first(measure_text(col, agg, adjust_age)).replace(
        " per ", "<br>per ", 1
    )


def add_whiskers(fig, x, y, plus, minus, row, col):

    """
    Draws interval whiskers as a dark outline under a white core,
    so they stand out on every bar color and in light or dark mode.
    """

    layers = [
        (5, 8, "rgba(0,0,0,0.85)"),
        (2, 5, "rgba(255,255,255,1)")
    ]

    for thickness, width, color in layers:

        fig.add_trace(
            go.Scatter(
                x=x,
                y=y,
                mode="markers",
                marker=dict(size=1, opacity=0),
                error_x=dict(
                    type="data",
                    symmetric=False,
                    array=plus,
                    arrayminus=minus,
                    thickness=thickness,
                    width=width,
                    color=color
                ),
                hoverinfo="skip",
                showlegend=False
            ),
            row=row,
            col=col
        )


def create_small_multiples(
    stats,
    intervals,
    aggregation_label,
    agg_func,
    adjust_age,
    unit_suffix
):

    """
    One horizontal bar panel per metric. A narrow text column on
    the right lists each tag's number of questions.
    """

    num_panels = len(ATT_METRICS) + 1

    fig = make_subplots(
        rows=1,
        cols=num_panels,
        shared_yaxes=True,
        horizontal_spacing=0.03,
        column_widths=[1, 1, 1, 0.25],
        subplot_titles=list(ATT_METRICS.keys()) + ["Questions"]
    )

    tags = stats["tag_name"]

    for i, (label, col) in enumerate(ATT_METRICS.items(), start=1):

        fig.add_trace(
            go.Bar(
                x=stats[col],
                y=tags,
                orientation="h",
                marker_color=ATT_COLORS[label],
                customdata=stats["question_count"],
                hovertemplate=(
                    "%{y}"
                    f"<br>{capitalize_first(measure_text(col, agg_func, adjust_age))}: "
                    "%{x:,.2f}"
                    "<br>Questions: %{customdata:,}"
                    "<extra></extra>"
                ),
                showlegend=False
            ),
            row=1,
            col=i
        )

        if col in intervals:

            low, high = intervals[col]
            values = stats[col].to_numpy(dtype=float)

            add_whiskers(
                fig,
                values,
                tags,
                np.nan_to_num(np.maximum(high - values, 0)),
                np.nan_to_num(np.maximum(values - low, 0)),
                row=1,
                col=i
            )

        fig.update_xaxes(
            title_text=axis_title(col, agg_func, adjust_age),
            row=1,
            col=i
        )

    # Raw question counts, aligned with each tag's row.
    fig.add_trace(
        go.Scatter(
            x=[0.5] * len(stats),
            y=tags,
            mode="text",
            text=[f"{count:,}" for count in stats["question_count"]],
            textposition="middle center",
            hoverinfo="skip",
            showlegend=False
        ),
        row=1,
        col=num_panels
    )

    fig.update_xaxes(
        visible=False,
        range=[0, 1],
        row=1,
        col=num_panels
    )

    # Highest-ranked tag at the top.
    fig.update_yaxes(autorange="reversed")

    fig.update_layout(
        title=(
            f"Tag Attention ({aggregation_label}{unit_suffix})"
        ),
        height=max(400, 32 * len(stats) + 190),
        margin=dict(l=20, r=20, t=80, b=90)
    )

    return fig


def create_rank_heatmap(stats, num_ranked):

    """Tags x metrics grid; each cell is the tag's rank (1 = best)."""

    labels = list(ATT_ALL_METRICS.keys())

    z = stats[
        [f"{col}_rank" for col in ATT_ALL_METRICS.values()]
    ].to_numpy()

    fig = go.Figure(
        data=go.Heatmap(
            z=z,
            x=labels,
            y=stats["tag_name"],
            text=z,
            texttemplate="%{text}",
            colorscale="Blues_r",
            zmin=1,
            zmax=max(int(z.max()), 2),
            xgap=2,
            ygap=2,
            hovertemplate=(
                "%{y}"
                "<br>%{x} rank: %{z}"
                f" of {num_ranked}"
                "<extra></extra>"
            ),
            colorbar=dict(title="Rank")
        )
    )

    fig.update_yaxes(autorange="reversed")

    fig.update_xaxes(side="top")

    fig.update_layout(
        title=f"Rank by Metric (out of {num_ranked} eligible tags)",
        height=max(400, 30 * len(stats) + 140),
        margin=dict(l=20, r=20, t=100, b=40)
    )

    return fig


def create_quadrant_chart(
    stats,
    y_label,
    y_col,
    log_x,
    quadrant_agg,
    adjust_age
):

    """
    Question volume (x) vs. attention per question (y), split
    into four quadrants at the median of each axis.
    """

    plot_df = (
        stats
        .sort_values("question_count", ascending=False)
        .head(150)
        .copy()
    )

    # Label only the highest-volume tags so labels stay readable.
    plot_df["label"] = ""
    plot_df.iloc[:40, plot_df.columns.get_loc("label")] = (
        plot_df["tag_name"].iloc[:40]
    )

    x_mid = plot_df["question_count"].median()
    y_mid = plot_df[y_col].median()

    y_title = axis_title(y_col, quadrant_agg, adjust_age).replace("<br>", " ")

    fig = px.scatter(
        plot_df,
        x="question_count",
        y=y_col,
        text="label",
        hover_name="tag_name",
        log_x=log_x,
        labels={
            "question_count": (
                "Number of questions (log scale)"
                if log_x
                else "Number of questions"
            ),
            y_col: y_title
        },
        title=(
            "Question volume vs. "
            f"{measure_text(y_col, quadrant_agg, adjust_age)}"
        )
    )

    fig.update_traces(
        textposition="top center",
        marker=dict(size=10, color=ATT_COLORS[y_label])
    )

    fig.add_vline(
        x=x_mid, line_dash="dash", line_color="rgba(120,120,120,0.7)"
    )

    fig.add_hline(
        y=y_mid, line_dash="dash", line_color="rgba(120,120,120,0.7)"
    )

    corners = [
        (0.99, 0.99, "right", "top", "Popular & engaging"),
        (0.01, 0.99, "left", "top", "Niche but engaging"),
        (0.99, 0.01, "right", "bottom", "Popular, less engaging"),
        (0.01, 0.01, "left", "bottom", "Niche & quiet"),
    ]

    for x, y, x_anchor, y_anchor, label_text in corners:

        fig.add_annotation(
            xref="paper",
            yref="paper",
            x=x,
            y=y,
            xanchor=x_anchor,
            yanchor=y_anchor,
            text=f"<b>{label_text}</b>",
            showarrow=False,
            font=dict(color="rgba(100,100,100,0.9)", size=13)
        )

    if log_x:

        # Minor gridlines make the log spacing visible even when
        # the counts only span a narrow range.
        fig.update_xaxes(
            minor=dict(ticks="outside", showgrid=True)
        )

    fig.update_layout(height=600)

    return fig


def render_tag_attention():

    st.header("Which tags receive the most attention?")

    st.write(
        "Compare Stack Exchange tags by the score, comments, "
        "and views their questions receive."
    )

    questions = load_attention_questions()
    question_tags = load_question_tags()

    # "Today" for age calculations: the newest question in the data.
    as_of_date = questions["creation_date"].max()

    st.sidebar.header("Filters")

    min_date = questions["creation_date"].min().date()
    max_date = questions["creation_date"].max().date()

    date_range = st.sidebar.date_input(
        "Question creation date",
        value=(min_date, max_date),
        min_value=min_date,
        max_value=max_date,
        key="ta_dates",
        help="Only questions created in this range are included."
    )

    start_date, end_date = read_date_range(date_range, min_date, max_date)

    status_label = st.sidebar.selectbox(
        "Question status",
        options=list(STATUS_FILTERS.keys()),
        key="ta_status",
        help=(
            "Duplicate and closed questions can behave differently "
            "from the rest."
        )
    )

    age_label = st.sidebar.selectbox(
        "Age adjustment",
        options=list(AGE_OPTIONS.keys()),
        key="ta_age",
        help=(
            "Older questions have had more time to collect views, "
            "score, and comments. 'Per 30 days of question age' "
            "divides each question's numbers by its age (minimum "
            f"{MIN_AGE_DAYS} days) so old and new questions are "
            "comparable. Attention is front-loaded, so this slightly "
            "favors tags with newer questions; it works best "
            "together with the date range."
        )
    )

    adjust_age = AGE_OPTIONS[age_label]

    aggregation_label = st.sidebar.selectbox(
        "Measure",
        options=list(ATT_AGGREGATIONS.keys()),
        key="ta_measure",
        help=(
            "Totals favor tags with many questions. Average and "
            "median show attention per question; the median is "
            "less affected by a few viral questions."
        )
    )

    agg_func = ATT_AGGREGATIONS[aggregation_label]

    rank_label = st.sidebar.selectbox(
        "Rank tags by",
        options=list(ATT_ALL_METRICS.keys()),
        key="ta_rank",
        help=(
            "Combined: each tag is given a percentile rank (0-100) "
            "on score, comments, and views among all tags that pass "
            "the filters, and the three are averaged (same ordering "
            "as adding them up). Percentiles are used so views, "
            "which are far larger numbers, don't dominate."
        )
    )

    min_questions = st.sidebar.slider(
        "Minimum questions per tag",
        min_value=1,
        max_value=100,
        value=30,
        key="ta_min_questions",
        help="Hides tags with too few questions to compare fairly."
    )

    top_n = st.sidebar.slider(
        "Number of tags",
        min_value=3,
        max_value=40,
        value=15,
        key="ta_top_n"
    )

    unit_suffix = " per 30 days" if adjust_age else ""

    # ---- Aggregate by tag ----

    filtered_questions = questions[
        questions["creation_date"].dt.date.between(start_date, end_date)
    ]

    status_column = STATUS_FILTERS[status_label]

    if status_column is not None:

        filtered_questions = filtered_questions[
            ~filtered_questions[status_column]
        ]

    filtered_questions = filtered_questions.copy()

    if adjust_age:

        age_in_months = (
            (as_of_date - filtered_questions["creation_date"])
            .dt.days
            .clip(lower=MIN_AGE_DAYS)
            / 30
        )

        for metric_col in ATT_METRICS.values():

            filtered_questions[metric_col] = (
                filtered_questions[metric_col] / age_in_months
            )

    merged = filtered_questions.merge(
        question_tags,
        on="question_id",
        how="inner"
    )

    tag_stats = compute_tag_stats(merged, agg_func, min_questions)

    rank_col = ATT_ALL_METRICS[rank_label]

    top_stats = (
        tag_stats
        .sort_values(rank_col, ascending=False)
        .head(top_n)
        .reset_index(drop=True)
    )

    # The volume-vs-attention chart needs a per-question figure, so a
    # "Total" measure falls back to the average there.
    quadrant_agg = "mean" if agg_func == "sum" else agg_func

    quadrant_stats = (
        tag_stats
        if quadrant_agg == agg_func
        else compute_tag_stats(merged, quadrant_agg, min_questions)
    )

    # ---- Display ----

    if top_stats.empty:

        st.warning(
            "No tags meet the current filters. Try lowering the "
            "minimum questions per tag or widening the date range."
        )

        return

    col1, col2, col3 = st.columns(3)

    col1.metric("Questions", f"{len(filtered_questions):,}")
    col2.metric("Tags displayed", len(top_stats))
    col3.metric(
        f"Top tag by {rank_label.lower()}",
        top_stats.loc[0, "tag_name"]
    )

    st.caption(
        "Questions with several tags count toward each of their "
        "tags, so closely related tags (for example python and "
        "pandas) often look similar and their totals overlap."
    )

    st.divider()

    tab_compare, tab_heatmap, tab_quadrant = st.tabs(
        ["Compare metrics", "Rank heatmap", "Volume vs. attention"]
    )

    with tab_compare:

        caption = (
            "Each panel has its own scale, so no metric drowns out "
            f"the others. Tags are ordered by {rank_label.lower()}"
        )

        if rank_col == "combined":
            caption += (
                " (the average percentile rank across the three "
                "metrics; see the Rank heatmap)"
            )

        caption += (
            ". Score is net votes (upvotes minus downvotes). The "
            "column on the right is each tag's number of questions."
        )

        if agg_func != "sum":
            caption += (
                " White whiskers show a 95% bootstrap interval: a "
                "long whisker means the estimate is uncertain, so "
                "close bars with overlapping whiskers shouldn't be "
                "treated as a real difference."
            )

        st.caption(caption)

        st.plotly_chart(
            create_small_multiples(
                top_stats,
                compute_intervals(merged, top_stats, agg_func),
                aggregation_label,
                agg_func,
                adjust_age,
                unit_suffix
            ),
            width="stretch"
        )

    with tab_heatmap:

        st.caption(
            "Each cell is the tag's rank on that metric among all "
            f"{len(tag_stats)} tags that pass the filters "
            "(1 = most attention; darker = better). Reading across "
            "a row shows whether a tag is strong everywhere or "
            "only on one metric."
        )

        st.plotly_chart(
            create_rank_heatmap(top_stats, len(tag_stats)),
            width="stretch"
        )

    with tab_quadrant:

        quadrant_note = (
            "Dashed lines mark the median of each axis. Tags on the "
            "right are asked about often; tags near the top get more "
            "attention per question. The vertical axis follows "
            f"'Rank tags by' in the sidebar ({rank_label})."
        )

        if agg_func == "sum":
            quadrant_note += (
                " Since 'Total' mixes volume into attention, this "
                "chart uses the average per question instead."
            )

        st.caption(quadrant_note)

        quadrant_log_x = st.checkbox(
            "Log scale for question count",
            value=True,
            key="ta_quadrant_log",
            help=(
                "Helps when a few tags have far more questions than "
                "the rest. If all tags have similar counts, a log "
                "axis looks almost the same as a linear one."
            )
        )

        st.plotly_chart(
            create_quadrant_chart(
                quadrant_stats,
                rank_label,
                rank_col,
                quadrant_log_x,
                quadrant_agg,
                adjust_age
            ),
            width="stretch"
        )


# ============================================================
# PAGE 5: RESPONSE TIME
# ============================================================

# Response label -> column holding hours from question creation.
RT_EVENTS = {
    "First answer": "hours_to_first",
    "Accepted answer": "hours_to_accepted"
}

# Wording that changes with the selected response.
EVENT_TEXT = {
    "First answer": {
        "unanswered": "Unanswered",
        "done": "answered",
        "rate": "answer rate"
    },
    "Accepted answer": {
        "unanswered": "No accepted answer",
        "done": "accepted",
        "rate": "accepted rate"
    }
}

# Questions whose outcome cannot be known from the pulled data are
# left out of that response's statistics:
#   First answer    answers_incomplete: fewer answers were pulled than
#                   the question's answer_count, so the earliest
#                   pulled answer may not be the real first one (or
#                   the question looks unanswered when it was not).
#   Accepted answer accepted_missing: the question has an accepted
#                   answer ID but that answer was never pulled.
EXCLUDE_COLS = {
    "First answer": "answers_incomplete",
    "Accepted answer": "accepted_missing"
}

# Time unit label -> number of hours in one unit.
TIME_UNITS = {
    "Minutes": 1 / 60,
    "Hours": 1,
    "Days": 24
}

# Tag sort key -> (column in the tag table, ascending?).
# Labels come from sort_labels() so they follow the selected response.
RT_SORTS = {
    "fastest_median": ("median", True),
    "slowest_median": ("median", False),
    "slowest_p90": ("p90", False),
    "highest_fast_share": ("within_fast", False),
    "most_questions": ("question_count", False),
    "highest_rate": ("answer_rate", False)
}

# "Answered within" share shown in the table and used for sorting.
FAST_HOURS = 24

# By default the date range ends this many days before the latest
# question, so recent questions have had time to be answered.
SETTLE_DAYS = 30

# Reference lines on the "answered within" chart, in hours.
THRESHOLDS = {
    "1 hour": 1,
    "1 day": 24,
    "1 week": 24 * 7
}

BOX_COLOR = "#4C78A8"
BAR_COLOR = "#E45756"

MAX_CURVE_TAGS = 8


def sort_labels(event_label):

    """Sort option labels that follow the selected response."""

    event_words = EVENT_TEXT[event_label]

    return {
        "fastest_median": "Fastest median",
        "slowest_median": "Slowest median",
        "slowest_p90": "Slowest 90th percentile",
        "highest_fast_share": (
            f"Highest % {event_words['done']} within {FAST_HOURS}h"
        ),
        "most_questions": "Most questions",
        "highest_rate": f"Highest {event_words['rate']}"
    }


def create_box_chart(stats, event_label, unit_label, log_scale):

    """
    One horizontal box per tag, with the unanswered count in a
    side panel on the same row. Whiskers are the Tukey fences
    (1.5 * IQR beyond the quartiles), clamped to the data range.
    """

    unanswered_label = EVENT_TEXT[event_label]["unanswered"]

    fig = make_subplots(
        rows=1,
        cols=2,
        shared_yaxes=True,
        column_widths=[0.78, 0.22],
        horizontal_spacing=0.03,
        subplot_titles=[
            f"Time to {event_label.lower()} ({unit_label.lower()})",
            unanswered_label
        ]
    )

    fig.add_trace(
        go.Box(
            y=stats["tag_name"],
            q1=stats["q1"],
            median=stats["median"],
            q3=stats["q3"],
            lowerfence=stats["lower_fence"],
            upperfence=stats["upper_fence"],
            orientation="h",
            marker_color=BOX_COLOR,
            name="",
            showlegend=False
        ),
        row=1,
        col=1
    )

    unanswered_text = [
        f"{count:,} ({1 - rate:.0%})"
        for count, rate in zip(
            stats["unanswered_count"],
            stats["answer_rate"]
        )
    ]

    fig.add_trace(
        go.Bar(
            x=stats["unanswered_count"],
            y=stats["tag_name"],
            orientation="h",
            marker_color=BAR_COLOR,
            text=unanswered_text,
            textposition="outside",
            cliponaxis=False,
            customdata=stats["question_count"],
            hovertemplate=(
                "%{y}"
                f"<br>{unanswered_label}: "
                "%{x:,}"
                "<br>Total questions: %{customdata:,}"
                "<extra></extra>"
            ),
            showlegend=False
        ),
        row=1,
        col=2
    )

    # Highest-ranked tag at the top.
    fig.update_yaxes(
        categoryorder="array",
        categoryarray=stats["tag_name"].tolist(),
        autorange="reversed"
    )

    fig.update_xaxes(
        type="log" if log_scale else "linear",
        row=1,
        col=1
    )

    # Leave room for the labels at the end of the bars.
    max_unanswered = max(float(stats["unanswered_count"].max()), 1.0)

    fig.update_xaxes(
        range=[0, max_unanswered * 1.5],
        showgrid=False,
        row=1,
        col=2
    )

    fig.update_layout(
        title=f"Time to {event_label} by Tag",
        height=max(400, 32 * len(stats) + 140),
        template="plotly_white",
        margin=dict(l=20, r=20, t=90, b=40)
    )

    return fig


def create_cumulative_chart(
    data,
    tags,
    hours_col,
    event_label,
    unit_label,
    unit_hours,
    log_scale
):

    """
    Percent of each tag's questions answered within a given time.
    Unanswered questions stay in the denominator, so every curve
    levels off at that tag's answer rate.
    """

    subset = data[data["tag_name"].isin(tags)]

    max_hours = float(subset[hours_col].max())

    if np.isnan(max_hours):
        max_hours = 2 * MIN_HOURS

    max_hours = max(max_hours, 2 * MIN_HOURS)

    if log_scale:

        grid = np.logspace(
            np.log10(MIN_HOURS),
            np.log10(max_hours),
            200
        )

    else:

        # Cap the normal scale at the 95th percentile so the long
        # tail does not flatten every curve.
        upper = float(subset[hours_col].quantile(0.95))

        if np.isnan(upper):
            upper = 2 * MIN_HOURS

        upper = max(upper, 2 * MIN_HOURS)

        grid = np.linspace(0, upper, 200)

    fig = go.Figure()

    for tag in tags:

        tag_data = data[data["tag_name"] == tag]

        total = tag_data["question_id"].nunique()

        if total == 0:
            continue

        times = np.sort(tag_data[hours_col].dropna().to_numpy())

        percent_answered = (
            np.searchsorted(times, grid, side="right") / total * 100
        )

        fig.add_trace(
            go.Scatter(
                x=grid / unit_hours,
                y=percent_answered,
                mode="lines",
                name=tag,
                hovertemplate=(
                    f"{tag}"
                    f"<br>Within %{{x:,.1f}} {unit_label.lower()}: "
                    "%{y:.1f}%"
                    "<extra></extra>"
                )
            )
        )

    # Reference lines at common thresholds.
    for threshold_label, threshold_hours in THRESHOLDS.items():

        if grid[0] <= threshold_hours <= grid[-1]:

            fig.add_vline(
                x=threshold_hours / unit_hours,
                line_dash="dot",
                line_width=1,
                line_color="gray",
                annotation_text=threshold_label,
                annotation_position="top"
            )

    fig.update_layout(
        title=f"Questions Receiving a {event_label} Within a Given Time",
        height=600,
        template="plotly_white",
        hovermode="x unified",
        xaxis_title=f"Time since question was asked ({unit_label.lower()})",
        yaxis_title="% of questions",
        yaxis=dict(range=[0, 100]),
        legend_title="Tag"
    )

    fig.update_xaxes(
        type="log" if log_scale else "linear"
    )

    return fig


def create_trend_chart(
    data,
    tags,
    hours_col,
    event_label,
    unit_label,
    unit_hours,
    log_scale,
    min_per_month
):

    """
    Median response time by question creation month, one line per
    tag, with the important events marked. Months with fewer than
    min_per_month answered questions are left out as too noisy.
    Returns None when nothing is left to draw.
    """

    answered = data[
        data["tag_name"].isin(tags) & data[hours_col].notna()
    ].copy()

    answered["month"] = (
        answered["creation_date"]
        .dt.tz_localize(None)
        .dt.to_period("M")
        .dt.to_timestamp()
    )

    monthly = (
        answered.groupby(["tag_name", "month"])[hours_col]
        .agg(median="median", answered="count")
        .reset_index()
    )

    monthly = monthly[monthly["answered"] >= min_per_month]

    if monthly.empty:

        return None

    fig = go.Figure()

    for tag in tags:

        tag_monthly = monthly[monthly["tag_name"] == tag]

        if tag_monthly.empty:

            continue

        fig.add_trace(
            go.Scatter(
                x=tag_monthly["month"],
                y=tag_monthly["median"] / unit_hours,
                mode="lines+markers",
                name=tag,
                customdata=tag_monthly["answered"],
                hovertemplate=(
                    "%{y:,.1f} " + unit_label.lower()
                    + " (%{customdata:,} answered)"
                    + "<extra>" + tag + "</extra>"
                )
            )
        )

    # Mark events that fall inside the plotted months.
    first_month = monthly["month"].min()
    last_month = monthly["month"].max() + pd.offsets.MonthEnd(1)

    for date, details in ACTIVITY_EVENTS.items():

        event_date = pd.Timestamp(date)

        if first_month <= event_date <= last_month:

            fig.add_shape(
                type="line",
                x0=event_date,
                x1=event_date,
                y0=0,
                y1=1,
                yref="paper",
                line=dict(
                    color=details["color"],
                    dash="dash",
                    width=2
                )
            )

            fig.add_annotation(
                x=event_date,
                y=1,
                yref="paper",
                text=details["label"],
                showarrow=False,
                textangle=-90,
                xanchor="left",
                yanchor="top",
                font=dict(size=10, color=details["color"])
            )

    fig.update_layout(
        title=f"Median Time to {event_label} by Month Asked",
        height=600,
        template="plotly_white",
        hovermode="x unified",
        xaxis_title="Month question was asked",
        yaxis_title=f"Median time ({unit_label.lower()})",
        legend_title="Tag"
    )

    fig.update_xaxes(tickformat="%b %Y")

    fig.update_yaxes(
        type="log" if log_scale else "linear"
    )

    return fig


def render_response_time():

    st.header("How long until questions get answered?")

    st.write(
        "Compare how quickly questions receive a first answer or "
        "an accepted answer, by tag."
    )

    questions = load_response_questions()
    question_tags = load_question_tags()

    # ---- Sidebar filters ----

    st.sidebar.header("Filters")

    min_date = questions["creation_date"].min().date()
    max_date = questions["creation_date"].max().date()

    # Default the range to end SETTLE_DAYS before the latest question.
    default_end = max(
        min_date,
        (pd.Timestamp(max_date) - pd.Timedelta(days=SETTLE_DAYS)).date()
    )

    date_range = st.sidebar.date_input(
        "Question creation date",
        value=(min_date, default_end),
        min_value=min_date,
        max_value=max_date,
        key="rt_dates",
        help=(
            "Recent questions have had less time to be answered, "
            "so they can make response times look faster and "
            "answer rates look lower. The range ends "
            f"{SETTLE_DAYS} days before the latest question by "
            "default; move the end date later to include newer "
            "questions."
        )
    )

    start_date, end_date = read_date_range(date_range, min_date, default_end)

    event_label = st.sidebar.selectbox(
        "Response",
        options=list(RT_EVENTS.keys()),
        key="rt_event",
        help=(
            "Accepted answer is the time until the answer that was "
            "eventually accepted was posted, not the time it was "
            "accepted."
        )
    )

    hours_col = RT_EVENTS[event_label]
    event_text = EVENT_TEXT[event_label]

    status_label = st.sidebar.selectbox(
        "Question status",
        options=list(STATUS_FILTERS.keys()),
        key="rt_status",
        help=(
            "Duplicate and closed questions can be closed quickly "
            "and rarely get answers, which can distort timing."
        )
    )

    scale_label = st.sidebar.radio(
        "Time axis",
        options=["Log scale", "Normal scale"],
        key="rt_scale",
        help=(
            "Response times are heavily skewed: most questions are "
            "answered quickly and a few take weeks. A log scale keeps "
            "the fast tags readable."
        )
    )

    log_scale = scale_label == "Log scale"

    unit_label = st.sidebar.selectbox(
        "Time unit",
        options=list(TIME_UNITS.keys()),
        index=1,
        key="rt_unit"
    )

    unit_hours = TIME_UNITS[unit_label]

    labels_for_sort = sort_labels(event_label)

    sort_key = st.sidebar.selectbox(
        "Rank tags by",
        options=list(RT_SORTS.keys()),
        format_func=lambda key: labels_for_sort[key],
        key="rt_sort"
    )

    min_answered = st.sidebar.slider(
        "Minimum answered questions per tag",
        min_value=1,
        max_value=100,
        value=10,
        key="rt_min_answered",
        help=(
            "Hides tags with too few answered questions for a "
            "reliable box."
        )
    )

    top_n = st.sidebar.slider(
        "Number of tags",
        min_value=3,
        max_value=40,
        value=15,
        key="rt_top_n"
    )

    # ---- Aggregate by tag ----

    filtered_questions = questions[
        questions["creation_date"].dt.date.between(start_date, end_date)
    ]

    status_column = STATUS_FILTERS[status_label]

    if status_column is not None:

        filtered_questions = filtered_questions[
            ~filtered_questions[status_column]
        ]

    # Leave out questions whose outcome cannot be known from the pulled
    # data (see EXCLUDE_COLS) instead of counting them as unanswered.
    exclude_col = EXCLUDE_COLS[event_label]

    excluded_count = int(filtered_questions[exclude_col].sum())

    usable_questions = filtered_questions[~filtered_questions[exclude_col]]

    clipped_count = int(
        usable_questions[CLIPPED_COLS[hours_col]].sum()
    )

    merged = usable_questions.merge(
        question_tags,
        on="question_id",
        how="inner"
    )

    # Share of each tag's questions with the response inside FAST_HOURS.
    # Unanswered questions stay in the denominator.
    merged = merged.assign(
        within_fast=merged[hours_col].le(FAST_HOURS)
    )

    grouped_hours = merged.groupby("tag_name")[hours_col]

    all_tag_stats = pd.DataFrame({
        "question_count": merged.groupby("tag_name")["question_id"].nunique(),
        "answered_count": grouped_hours.count(),
        "min": grouped_hours.min(),
        "q1": grouped_hours.quantile(0.25),
        "median": grouped_hours.quantile(0.50),
        "q3": grouped_hours.quantile(0.75),
        "p90": grouped_hours.quantile(0.90),
        "max": grouped_hours.max(),
        "within_fast": merged.groupby("tag_name")["within_fast"].mean()
    })

    # Tukey fences: 1.5 * IQR beyond the quartiles, used directly as
    # the whisker bounds so no outliers need to be identified.
    # Clamped to the observed range so a fence never extends past the
    # data, and never reaches zero or below (a log axis cannot show it).
    iqr = all_tag_stats["q3"] - all_tag_stats["q1"]

    all_tag_stats["lower_fence"] = (
        (all_tag_stats["q1"] - 1.5 * iqr)
        .clip(lower=all_tag_stats["min"])
    )

    all_tag_stats["upper_fence"] = (
        (all_tag_stats["q3"] + 1.5 * iqr)
        .clip(upper=all_tag_stats["max"])
    )

    all_tag_stats["unanswered_count"] = (
        all_tag_stats["question_count"] - all_tag_stats["answered_count"]
    )

    all_tag_stats["answer_rate"] = (
        all_tag_stats["answered_count"] / all_tag_stats["question_count"]
    )

    all_tag_stats = all_tag_stats.reset_index()

    all_tag_stats = all_tag_stats[
        all_tag_stats["answered_count"] >= min_answered
    ]

    sort_col, sort_ascending = RT_SORTS[sort_key]

    tag_stats = (
        all_tag_stats
        .sort_values(sort_col, ascending=sort_ascending)
        .head(top_n)
        .reset_index(drop=True)
    )

    # Convert the time columns to the chosen unit for display.
    TIME_COLS = ["lower_fence", "q1", "median", "q3", "p90", "upper_fence"]

    display_stats = tag_stats.copy()

    display_stats[TIME_COLS] = display_stats[TIME_COLS] / unit_hours

    # ---- Display ----

    if tag_stats.empty:

        st.warning(
            "No tags meet the current filters. Try lowering the "
            "minimum answered questions per tag or widening the date "
            "range."
        )

        return

    answered_share = usable_questions[hours_col].notna().mean()

    col1, col2, col3 = st.columns(3)

    col1.metric("Questions", f"{len(usable_questions):,}")
    col2.metric("Tags displayed", len(tag_stats))
    col3.metric(
        f"Questions with {event_label.lower()}",
        f"{answered_share:.1%}"
    )

    st.divider()

    if event_label == "Accepted answer":

        st.caption(
            "Time is measured until the accepted answer was "
            "posted, not until it was accepted. "
            f"{excluded_count:,} questions have an accepted answer "
            "that was never pulled; their time is unknown, so they "
            "are left out rather than counted as having no "
            "accepted answer."
        )

    else:

        st.caption(
            f"{excluded_count:,} questions have fewer answers "
            "pulled than their answer count says. Their first "
            "answer may be missing or they may look unanswered, so "
            "they are left out."
        )

    st.caption(
        "Questions are filtered by creation date. Recent questions "
        "have had less time to receive answers, so their response "
        "times look faster and their unanswered counts look higher."
    )

    st.caption(
        "A question with several tags is counted in every one of "
        "its tags, so tag statistics overlap and should not be "
        "added together."
    )

    if clipped_count > 0:

        st.caption(
            f"{clipped_count:,} response times under one minute "
            "(including zero or negative times) were set to one "
            "minute so they can appear on the log axis."
        )

    curve_options = (
        all_tag_stats
        .sort_values("question_count", ascending=False)["tag_name"]
        .tolist()
    )

    tab_box, tab_curve, tab_trend = st.tabs(
        ["Response times", "Answered within...", "Trend over time"]
    )

    with tab_box:

        st.caption(
            "Each box spans the 25th to 75th percentile with the "
            "median marked inside. Whiskers extend 1.5 times the "
            "interquartile range beyond the box (Tukey fences), "
            "limited to the fastest and slowest observed times. "
            "Only questions with a response are in the boxes; the "
            "panel on the right counts the rest."
        )

        st.plotly_chart(
            create_box_chart(
                display_stats,
                event_label,
                unit_label,
                log_scale
            ),
            width="stretch"
        )

    with tab_curve:

        st.caption(
            "Shows the share of each tag's questions that got a "
            f"{event_label.lower()} within a given time. Curves "
            "level off at the tag's answer rate because "
            "unanswered questions are included."
        )

        curve_tags = st.multiselect(
            f"Select tags to compare (max {MAX_CURVE_TAGS}):",
            options=curve_options,
            default=curve_options[:5],
            max_selections=MAX_CURVE_TAGS,
            key="rt_curve_tags"
        )

        if curve_tags:

            st.plotly_chart(
                create_cumulative_chart(
                    merged,
                    curve_tags,
                    hours_col,
                    event_label,
                    unit_label,
                    unit_hours,
                    log_scale
                ),
                width="stretch"
            )

        else:

            st.info("Please select at least one tag to display the chart.")

    with tab_trend:

        st.caption(
            "Median time by the month each question was asked. "
            "Months with too few answered questions are hidden. "
            "Recent months look faster until answers have had "
            "time to arrive, so the date range ends earlier by "
            "default."
        )

        trend_tags = st.multiselect(
            f"Select tags to compare (max {MAX_CURVE_TAGS}):",
            options=curve_options,
            default=curve_options[:5],
            max_selections=MAX_CURVE_TAGS,
            key="rt_trend_tags"
        )

        min_per_month = st.slider(
            "Minimum answered questions per month",
            min_value=1,
            max_value=50,
            value=5,
            key="rt_trend_min_per_month",
            help="Hides months with too few questions for a stable median."
        )

        if trend_tags:

            trend_fig = create_trend_chart(
                merged,
                trend_tags,
                hours_col,
                event_label,
                unit_label,
                unit_hours,
                log_scale,
                min_per_month
            )

            if trend_fig is None:

                st.info(
                    "No months meet the minimum. Try lowering the "
                    "minimum answered questions per month."
                )

            else:

                st.plotly_chart(trend_fig, width="stretch")

        else:

            st.info("Please select at least one tag to display the chart.")

    with st.expander("Show data table"):

        table = display_stats[[
            "tag_name",
            "question_count",
            "answered_count",
            "answer_rate",
            "median",
            "q3",
            "p90",
            "within_fast"
        ]].copy()

        table["answer_rate"] = table["answer_rate"] * 100
        table["within_fast"] = table["within_fast"] * 100

        done = event_text["done"].capitalize()

        table = table.rename(columns={
            "tag_name": "Tag",
            "question_count": "Questions",
            "answered_count": done,
            "answer_rate": f"{event_text['rate'].capitalize()} (%)",
            "median": f"Median ({unit_label.lower()})",
            "q3": f"75th percentile ({unit_label.lower()})",
            "p90": f"90th percentile ({unit_label.lower()})",
            "within_fast": f"{done} within {FAST_HOURS}h (%)"
        })

        st.dataframe(
            table.round(2),
            width="stretch",
            hide_index=True
        )


# ============================================================
# MAIN: NAVIGATION (top of the page; the sidebar holds only filters)
# ============================================================

PAGES = {
    "Which tags are growing or shrinking?": render_tag_trends,
    "How has site activity changed?": render_post_activity,
    "Which tags appear together?": render_tag_relationships,
    "Which tags get the most attention?": render_tag_attention,
    "How long until questions are answered?": render_response_time
}

st.title("Stack Exchange Dashboard")

check_database()

nav_col, refresh_col = st.columns([6, 1])

with nav_col:
    selected_page = st.radio(
        "Question",
        list(PAGES.keys()),
        key="page",
        horizontal=True,
        label_visibility="collapsed"
    )

with refresh_col:
    # Results are cached for 10 minutes; use this right after running `update`.
    if st.button("Refresh data", width="stretch"):
        st.cache_data.clear()
        st.cache_resource.clear()

st.divider()

PAGES[selected_page]()

st.divider()

st.caption(
    "Data: Stack Exchange API (Stack Overflow), licensed CC BY-SA. "
    "Question titles and links are not shown; no user data is collected."
)