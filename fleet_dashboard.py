"""Streamlit-dashboard til analyse af flådens kørebog i Excel-format."""

import datetime
from datetime import timedelta
from typing import List, Optional
from zipfile import BadZipFile

import numpy as np
import pandas as pd
import plotly.express as px
import streamlit as st
from openpyxl.utils.exceptions import InvalidFileException


WORKDAY_START = datetime.time(8, 0)
WORKDAY_END = datetime.time(17, 0)
WORKDAY_HOURS = (datetime.datetime.combine(datetime.date.min, WORKDAY_END) -
                 datetime.datetime.combine(datetime.date.min, WORKDAY_START)).seconds / 3600.0

WEEKDAY_LABELS_DA = {
    0: "Mandag",
    1: "Tirsdag",
    2: "Onsdag",
    3: "Torsdag",
    4: "Fredag",
    5: "Lørdag",
    6: "Søndag",
}
WEEKDAY_ORDER = [WEEKDAY_LABELS_DA[i] for i in range(7)]


def summarize_locations(df_home: pd.DataFrame, col_vehicle="license_plate") -> pd.DataFrame:
    """
    Grupperer på 'Home location' og tæller antal køretøjer + samler reg.nr. i en kommasepareret liste.
    """
    if df_home.empty:
        return pd.DataFrame(columns=["Home location", "Vehicles", "license_plate"])

    agg = (df_home
           .groupby("Home location", dropna=True)
           .agg(
               Vehicles=(col_vehicle, "nunique"),
               Registreringsnumre=(col_vehicle, lambda s: ", ".join(sorted(map(str, s.unique()))))
           )
           .reset_index()
           .sort_values("Vehicles", ascending=False))
    return agg


def compute_overview_metrics(df: pd.DataFrame, num_days) -> pd.DataFrame:
    """Compute summary metrics by start location and fuel.

    Returns a DataFrame with the following columns:

    * start_lokation: trip start location
    * drivmiddel: fuel category
    * trips: total number of trips in the period
    * total_km: total kilometres driven in the period
    * total_duration: total duration in hours
    * unique_vehicles: number of distinct vehicles
    * avg_trips_per_day: average trips per day
    * avg_km_per_day: average kilometres per day
    """
    grouped = (
        df.groupby(["start_lokation", "drivmiddel"], dropna=False)
        .agg(
            trips=("license_plate", "count"),
            total_km=("distance_km", "sum"),
            total_duration=("duration_hours", "sum"),
            unique_vehicles=("license_plate", "nunique"),
        )
        .reset_index()
    )
    grouped["avg_trips_per_day"] = grouped["trips"] / num_days
    grouped["avg_km_per_day"] = grouped["total_km"] / num_days
    grouped["num_days"] = num_days  # for reference/debugging
    return grouped


def _overlap_hours(a_start, a_end, b_start, b_end) -> float:
    """Returnér antal timer i overlap mellem [a_start, a_end) og [b_start, b_end)."""
    start = max(a_start, b_start)
    end = min(a_end, b_end)
    return max(0.0, (end - start).total_seconds() / 3600.0)


COLS = {
    "reg": "license_plate",     # fx "AB12345"
    "locations": "start_lokation",  # fx "Hovedkontor"
    "start": "start",        # fx "2025-06-14 16:00"
    "end": "end"            # fx "2025-06-15 16:00"
}


def compute_daily_utilization(
    df: pd.DataFrame,
    reg_col: str = COLS["reg"],
    loc_col: str = COLS["locations"],
    start_col: str = COLS["start"],
    end_col: str = COLS["end"],
) -> pd.DataFrame:
    """
    Splitter ture på tværs af datoer og beregner:
      - total brugte timer pr. dag pr. køretøj
      - overlap med 08-16 vinduet
      - udnyttelse i pct (min(hours/8, 1)*100)
      - flag for opfyldt min. 8 timer
    Returnerer en DataFrame på niveau (Dato, Registreringsnummer).
    """

    # 1) Datotyper + oprydning
    out = df.copy()
    out[start_col] = pd.to_datetime(out[start_col], errors="coerce")
    out[end_col] = pd.to_datetime(out[end_col], errors="coerce")

    # Drop ugyldige rækker
    out = out.dropna(subset=[reg_col, loc_col, start_col, end_col]).copy()

    # Sørg for start <= slut (swap hvis nødvendigt)
    mask_swap = out[end_col] < out[start_col]
    if mask_swap.any():
        tmp = out.loc[mask_swap, start_col].copy()
        out.loc[mask_swap, start_col] = out.loc[mask_swap, end_col]
        out.loc[mask_swap, end_col] = tmp

    # 2) Ekspandér ture til per-dag segmenter
    records = []
    for _, row in out.iterrows():
        reg = row[reg_col]
        s = row[start_col]
        e = row[end_col]
        l = row[loc_col]

        # Iterér dag for dag
        cur = s.normalize()  # dagsstart (00:00) for start-dagen
        last_day = e.normalize()
        while cur <= last_day:
            day_start = max(s, cur)
            day_end = min(e, cur + pd.Timedelta(days=1))

            hours_total = (day_end - day_start).total_seconds() / 3600.0

            # Kontortids-overlap (08-16 for dagens dato)
            ws = datetime.datetime.combine(cur.date(), WORKDAY_START)
            we = datetime.datetime.combine(cur.date(), WORKDAY_END)
            hours_in_window = _overlap_hours(day_start, day_end, ws, we)

            records.append({
                "Dato": cur.date(),
                reg_col: reg,
                loc_col: l,
                "Timer_total": hours_total,
                "Timer_08_17": hours_in_window
            })

            cur += pd.Timedelta(days=1)

    if not records:
        return pd.DataFrame(columns=["Dato", reg_col, loc_col, "Timer_total", "Timer_08_17",
                                     "Udnyttelse_pct", "Opfyldt_min_8t", "Udnyttelse_pct_08_16"])

    daily = pd.DataFrame.from_records(records)

    return daily


def compute_weekday_metrics(
    df: pd.DataFrame,
    start_date: Optional[datetime.date] = None,
    end_date: Optional[datetime.date] = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Return aggregated metrics pr. ugedag for lokationer og samlet set.

    Parameters
    ----------
    df : pandas.DataFrame
        Allerede filtreret dataset.
    start_date, end_date : datetime.date, optional
        De valgte datogrænser. Bruges til at beregne hvor mange gange hver
        ugedag optræder i intervallet (for gennemsnitlige ture pr. dag).

    Returns
    -------
    tuple[pandas.DataFrame, pandas.DataFrame]
        Første DataFrame indeholder kolonnerne::

            [start_lokation, weekday_num, weekday, trips, avg_trips_per_day,
             total_distance, avg_distance, total_duration, avg_duration,
             days_in_range]

        Anden DataFrame er en samlet oversigt pr. ugedag (uden lokation).
    """

    if df.empty:
        cols = [
            "start_lokation",
            "weekday_num",
            "weekday",
            "trips",
            "avg_trips_per_day",
            "total_distance",
            "avg_distance",
            "total_duration",
            "avg_duration",
            "days_in_range",
        ]
        empty_loc = pd.DataFrame(columns=cols)
        empty_overall = pd.DataFrame(
            columns=[
                "weekday_num",
                "weekday",
                "trips",
                "avg_trips_per_day",
                "avg_duration",
                "median_duration",
                "avg_distance",
                "median_distance",
            ]
        )
        return empty_loc, empty_overall

    work = df.copy()
    work["date"] = pd.to_datetime(work["date"], errors="coerce")
    work = work.dropna(subset=["date"]).copy()
    work["weekday_num"] = work["date"].dt.weekday
    work["weekday"] = work["weekday_num"].map(WEEKDAY_LABELS_DA)

    # Fastlæg datointervallet der skal bruges til gennemsnit
    start = pd.to_datetime(start_date) if start_date else work["date"].min()
    end = pd.to_datetime(end_date) if end_date else work["date"].max()
    if pd.isna(start) or pd.isna(end):
        date_range = pd.DatetimeIndex([])
    else:
        if start > end:
            start, end = end, start
        date_range = pd.date_range(start, end, freq="D")

    weekday_calendar = (
        pd.DataFrame({"weekday_num": range(7)})
        .assign(days_in_range=0)
    )
    if not date_range.empty:
        weekday_counts = (
            pd.Series(date_range.weekday)
            .value_counts()
            .reindex(range(7), fill_value=0)
            .rename("days_in_range")
            .rename_axis("weekday_num")
            .reset_index()
        )
        weekday_calendar = weekday_counts

    agg_kwargs = {"trips": ("license_plate", "count")}
    if "distance_km" in work.columns:
        agg_kwargs["total_distance"] = ("distance_km", "sum")
        agg_kwargs["avg_distance"] = ("distance_km", "mean")
    if "duration_hours" in work.columns:
        agg_kwargs["total_duration"] = ("duration_hours", "sum")
        agg_kwargs["avg_duration"] = ("duration_hours", "mean")

    grouped = (
        work.groupby(["start_lokation", "weekday_num", "weekday"], dropna=False)
        .agg(**agg_kwargs)
        .reset_index()
    )
    grouped = grouped.merge(weekday_calendar, on="weekday_num", how="left")
    grouped["days_in_range"] = grouped["days_in_range"].fillna(0)
    grouped["avg_trips_per_day"] = np.where(
        grouped["days_in_range"] > 0,
        grouped["trips"] / grouped["days_in_range"],
        np.nan,
    )

    overall_agg_kwargs = {"trips": ("license_plate", "count")}
    if "duration_hours" in work.columns:
        overall_agg_kwargs["avg_duration"] = ("duration_hours", "mean")
        overall_agg_kwargs["median_duration"] = ("duration_hours", "median")
    if "distance_km" in work.columns:
        overall_agg_kwargs["avg_distance"] = ("distance_km", "mean")
        overall_agg_kwargs["median_distance"] = ("distance_km", "median")

    overall = (
        work.groupby(["weekday_num", "weekday"], dropna=False)
        .agg(**overall_agg_kwargs)
        .reset_index()
    )
    overall = overall.merge(weekday_calendar, on="weekday_num", how="left")
    overall["days_in_range"] = overall["days_in_range"].fillna(0)
    overall["avg_trips_per_day"] = np.where(
        overall["days_in_range"] > 0,
        overall["trips"] / overall["days_in_range"],
        np.nan,
    )

    return grouped, overall



def filter_data(
    df: pd.DataFrame,
    lokationer: Optional[List[str]] = None,
    vehicles: Optional[List[str]] = None,
    fuels: Optional[List[str]] = None,
    start_date: Optional[datetime.date] = None,
    end_date: Optional[datetime.date] = None,
) -> pd.DataFrame:
    """Filter trips by start location, vehicle, fuel and trip date."""
    mask = pd.Series(True, index=df.index)

    if lokationer:
        mask &= df["start_lokation"].isin(lokationer)

    if vehicles:
        mask &= df["license_plate"].isin(vehicles)

    if fuels:
        mask &= df["drivmiddel"].isin(fuels)

    if start_date:
        mask &= df["date"] >= start_date

    if end_date:
        mask &= df["date"] <= end_date

    return df[mask].copy()


TRIP_COLUMNS = (
    "date",
    "start",
    "end",
    "start_lokation",
    "license_plate",
    "drivmiddel",
    "distance_km",
    "duration_hours",
)


def load_trip_workbook(uploaded_file) -> pd.DataFrame:
    """Læs og validér første ark i en Excel-kørebog."""
    try:
        data = pd.read_excel(uploaded_file, engine="openpyxl", dtype={"license_plate": str})
    except (ValueError, OSError, BadZipFile, InvalidFileException) as exc:
        raise ValueError("Excel-filen kunne ikke læses. Kontrollér, at den er en gyldig .xlsx-fil.") from exc

    data.columns = data.columns.str.strip()
    missing = [column for column in TRIP_COLUMNS if column not in data]
    if missing:
        raise ValueError(f"Første ark mangler kolonnerne: {', '.join(missing)}.")

    data = data.loc[:, list(TRIP_COLUMNS)].dropna(how="all").copy()
    if data.empty:
        raise ValueError("Første ark indeholder ingen ture.")

    for column in ("date", "start", "end"):
        data[column] = pd.to_datetime(data[column], errors="coerce", dayfirst=True, format="mixed")
    data["date"] = data["date"].dt.date

    for column in ("distance_km", "duration_hours"):
        data[column] = pd.to_numeric(data[column], errors="coerce")
        data[column] = data[column].where(np.isfinite(data[column]) & data[column].ge(0))

    for column in ("license_plate", "start_lokation", "drivmiddel"):
        data[column] = data[column].astype("string").str.strip().replace("", pd.NA)
    data["start_lokation"] = data["start_lokation"].fillna("Ukendt lokation")
    data["drivmiddel"] = data["drivmiddel"].fillna("Ukendt drivmiddel")

    invalid = data.isna().any(axis=1) | data["end"].lt(data["start"])
    if invalid.any():
        raise ValueError(
            f"{invalid.sum():,} rækker har manglende eller ugyldige turdata. "
            "Kontrollér dato, start, slut, registreringsnummer, distance og varighed."
        )
    return data


def main():
    """Start flådestyringsdashboardet."""
    st.set_page_config(
        page_title="Flådestyringsdashboard",
        layout="wide",
        initial_sidebar_state="expanded",
    )

    st.title("Flådestyringsdashboard")
    uploaded_file = st.sidebar.file_uploader("Upload kørebog (Excel)", type=["xlsx"])
    if uploaded_file is None:
        st.session_state.pop("trip_data", None)
        st.session_state.pop("trip_file_id", None)
        st.info("Upload en Excel-kørebog i sidepanelet for at se flådens anvendelse.")
        return

    # Streamlit giver hver upload sit eget ID, så filtre kan ændres uden at genlæse filen.
    if st.session_state.get("trip_file_id") != uploaded_file.file_id:
        try:
            data = load_trip_workbook(uploaded_file)
        except ValueError as exc:
            st.session_state.pop("trip_data", None)
            st.session_state.pop("trip_file_id", None)
            st.error(str(exc))
            return
        st.session_state["trip_data"] = data
        st.session_state["trip_file_id"] = uploaded_file.file_id

    data = st.session_state["trip_data"]
    file_id = uploaded_file.file_id
    st.sidebar.header("Filtre")
    selected_lokations = st.sidebar.multiselect(
        "Vælg startlokationer", sorted(data["start_lokation"].unique()), key=f"locations_{file_id}"
    )
    selected_vehicles = st.sidebar.multiselect(
        "Vælg køretøjer", sorted(data["license_plate"].unique()), key=f"vehicles_{file_id}"
    )
    selected_fuels = st.sidebar.multiselect(
        "Vælg drivmiddel", sorted(data["drivmiddel"].unique()), key=f"fuels_{file_id}"
    )

    min_date, max_date = data["date"].min(), data["date"].max()
    date_range = st.sidebar.date_input(
        "Dato-interval", value=(min_date, max_date), min_value=min_date, max_value=max_date,
        key=f"dates_{file_id}",
    )
    if len(date_range) != 2:
        st.info("Vælg en start- og slutdato.")
        return
    start_date, end_date = date_range

    filtered = filter_data(data, selected_lokations, selected_vehicles, selected_fuels, start_date, end_date)
    if filtered.empty:
        st.info("Ingen ture i den valgte periode med de aktuelle filtre.")
        return

    num_days = (end_date - start_date).days + 1
    overview = compute_overview_metrics(filtered, num_days)
    daily_util = compute_daily_utilization(filtered)
    daily_util = daily_util[daily_util["Dato"].between(start_date, end_date)]

    st.sidebar.caption("Dagsgennemsnit beregnes over alle kalenderdage i den valgte periode.")

    tabs = st.tabs(
        [
            "Oversigt",
            "Lokationer & drivmidler",
            "Udnyttelsesgrad over tid",
            "Ugedagsanvendelse",
        ]
    )

    # Overview tab
    with tabs[0]:
        st.header("Oversigt")
        st.markdown(
            """
            Her kan du se de samlede nøgletal for de valgte filtre.  "Trips"
            angiver antal ture, "km" er de samlede kilometre, og
            "udnyttelsesgrad" viser forholdet mellem faktisk køretid og den
            teoretisk tilgængelige arbejdstid.
            """
        )

        # Display key metrics
        st.subheader("Nøgletal for valgt periode/filtre")

        # KPI: unikke køretøjer, lokationer, samlede km, ture, gns. udnyttelse (8–17)

        unique_vehicles = filtered['license_plate'].nunique(dropna=True)
        unique_locations = overview["start_lokation"].nunique(dropna=True)
        total_km = overview["total_km"].sum()
        total_trips = overview["trips"].sum()
        total_time = daily_util["Timer_08_17"].sum()
        utilisation = min(total_time / (unique_vehicles * num_days * WORKDAY_HOURS), 1.0) * 100

        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric("Antal køretøjer", f"{unique_vehicles:,}")
        c2.metric("Lokationer", f"{unique_locations:,}")
        c3.metric("Samlede km", f"{total_km:,.0f}".replace(",", "."))
        c4.metric("Ture", f"{total_trips:,.0f}".replace(",", "."))
        c5.metric("Gns. udnyttelse pr. dag(8–17)", f"{utilisation:.1f}%")

        # Display metrics as table

        st.subheader("Køretøjer pr. drivmiddel")
        vehicle_counts = (
            filtered.groupby("drivmiddel")["license_plate"]
            .nunique()
            .sort_values(ascending=False)
            .reset_index(name="Antal unikke køretøjer")
        )
        st.dataframe(vehicle_counts.rename(columns={"drivmiddel": "Drivmiddel"}), hide_index=True)

        # Chart: average trips per day by location and vehicle type
        if not overview.empty:
            fig1 = px.bar(
                overview,
                x="start_lokation",
                y="avg_trips_per_day",
                color="drivmiddel",
                barmode="group",
                labels={
                    "start_lokation": "Lokation",
                    "avg_trips_per_day": "Gennemsnitlige ture pr. dag",
                    "drivmiddel": "Drivmiddel",
                },
                title="Gennemsnitlige ture pr. dag pr. lokation og drivmiddel",
            )
            st.plotly_chart(fig1, use_container_width=True)

    # Location & vehicle type tab
    with tabs[1]:
        st.header("Lokationer & køretøj")

        st.markdown("Nøgletal pr. lokation og drivmiddel.")
        st.dataframe(overview, use_container_width=True)
        # Chart: utilisation by location and vehicle type
        if not overview.empty:
            fig2 = px.bar(
                overview,
                x="start_lokation",
                y="unique_vehicles",
                color="drivmiddel",
                barmode="group",
                labels={
                    "start_lokation": "Lokation",
                    "unique_vehicles": "Antal køretøjer",
                    "drivmiddel": "Drivmiddel",
                },
                title="Antal køretøjer pr. lokation og drivmiddel",
            )
            st.plotly_chart(fig2, use_container_width=True)

        st.subheader("Registrerede startlokationer pr. køretøj")
        home_df = filtered[["license_plate", "start_lokation"]].drop_duplicates().rename(
            columns={"start_lokation": "Home location"}
        )

        loc_agg = summarize_locations(home_df, col_vehicle="license_plate")

        # Top-metrics
        c1, c2 = st.columns(2)
        total_locs = int(loc_agg["Home location"].nunique()) if not loc_agg.empty else 0
        total_veh  = int(home_df["license_plate"].nunique()) if not home_df.empty else 0
        with c1:
            st.metric("Antal lokationer", total_locs)
        with c2:
            st.metric("Antal køretøjer", total_veh)

        # Bar chart: antal køretøjer pr. lokation
        if not loc_agg.empty:
            fig_loc = px.bar(
                loc_agg,
                x="Home location",
                y="Vehicles",
                title="Køretøjer pr. lokation",
            )
            fig_loc.update_layout(xaxis_title="Lokation", yaxis_title="Antal køretøjer")
            st.plotly_chart(fig_loc, use_container_width=True)
        else:
            st.info("Ingen lokationer fundet for det aktuelle filter.")

        # Tabel: lokation, antal, liste af reg.nr.
        with st.expander("Se tabel: Lokation → antal køretøjer → reg.nr."):
            st.dataframe(
                loc_agg.rename(columns={
                    "Home location": "Lokation",
                    "Vehicles": "Antal køretøjer",
                    "Registreringsnumre": "Registreringsnumre"
                }),
                use_container_width=True
            )

        # Tabel: køretøj → home location
        with st.expander("Se tabel: Køretøj → startlokation"):
            st.dataframe(
                home_df.rename(columns={
                    "license_plate": "Køretøj",
                    "Home location": "Startlokation"
                }).sort_values("Køretøj"),
                use_container_width=True
            )


    # Utilisation over time tab
    with tabs[2]:
        st.header("Udnyttelsesgrad over tid")
        st.markdown(
            "Gennemsnitlig udnyttelsesgrad pr. dag på tværs af alle valgte biler og lokationer."
        )
        if daily_util.empty:
            st.info("Ingen data til rådighed for de valgte filtre.")
        else:

            # Lokalt filter for denne side
            st.markdown("**Filtrering på drivmiddel (kun for denne visning)**")

            available_fuels = sorted([x for x in filtered["drivmiddel"].dropna().unique()])
            utilisation_fuels = st.multiselect(
                "Vælg drivmiddel(r)",
                options=available_fuels,
                default=available_fuels,
                key=f"utilisation_fuels_{file_id}"
            )

            # Filtrer data kun for denne side
            filtered_util = filtered[filtered["drivmiddel"].isin(utilisation_fuels or available_fuels)].copy()

            if len(filtered_util) != len(filtered):
                daily_util = compute_daily_utilization(filtered_util)
                daily_util = daily_util[daily_util["Dato"].between(start_date, end_date)]

            vehicle_count = filtered_util["license_plate"].nunique()
            daily_util = daily_util.assign(Dato=pd.to_datetime(daily_util["Dato"]))

            daily_util_agg = (
                daily_util.groupby("Dato", dropna=False)
                .agg(
                    hours_total=("Timer_total", "sum"),
                    hours_08_17=("Timer_08_17", "sum"),
                )
                .reset_index()
            )

            # 5) Lav en komplet kalender med alle datoer i intervallet
            all_dates = pd.DataFrame({"Dato": pd.date_range(start_date, end_date, freq="D")})

            # 6) Merge for at sikre alle datoer er med, og udfyld manglende med 0
            daily_util_agg = (
                all_dates.merge(daily_util_agg, on="Dato", how="left")
                .fillna({"hours_total": 0.0, "hours_08_17": 0.0})
            )

            daily_util_agg["avg_hours_total"] = daily_util_agg["hours_total"] / vehicle_count
            daily_util_agg["avg_hours_08_17"] = daily_util_agg["hours_08_17"] / vehicle_count

            # 8) Til reference: antal biler
            daily_util_agg["Antal biler"] = vehicle_count

            # 9) Udnyttelsesgrader (pr. køretøj)
            #    Fordi "avg_hours_*" allerede er pr. bil, skal der KUN divideres med WORKDAY_HOURS.
            daily_util_agg["udnyttelse_pct_08_17"] = (daily_util_agg["avg_hours_08_17"] / WORKDAY_HOURS).clip(upper=1.0) * 100.0

            # Beregn gennemsnit og 7-dages glidende gennemsnit
            mean_value = daily_util_agg["udnyttelse_pct_08_17"].mean()

            st.markdown(f"Gennemsnitlig udnyttelsesgrad (08–17) i perioden: **{mean_value:.1f}%**")

            daily_util_agg["rolling_mean_7d"] = (
                daily_util_agg["udnyttelse_pct_08_17"]
                .rolling(window=7, min_periods=1)
                .mean()
)

            # Linjediagram: Udnyttelsesgrad over tid (08-17)
            st.subheader("Udnyttelsesgrad over tid (08–17)")
            fig6 = px.line(
                daily_util_agg,
                x="Dato",
                y="udnyttelse_pct_08_17",
                labels={"udnyttelse_pct_08_17": "Udnyttelsesgrad (%)", "Dato": "Dato"},
                title="Gennemsnitlig udnyttelsesgrad over tid (08–17)"
            )

            # Tilføj 7-dages glidende gennemsnit
            fig6.add_scatter(
                x=daily_util_agg["Dato"],
                y=daily_util_agg["rolling_mean_7d"],
                mode="lines",
                name="7-dages gennemsnit",
                line=dict(color="orange", width=3, dash="dash")
            )

            # Tilføj en vandret linje for globalt gennemsnit
            fig6.add_hline(
                y=mean_value,
                line_dash="dot",
                line_color="green",
                name="Gennemsnit",
                annotation_text=f"Gennemsnit: {mean_value:.1f}%",
                annotation_position="top left"
            )

            fig6.update_traces(mode="markers+lines")
            st.plotly_chart(fig6, use_container_width=True)

            # Udnyttelsesgrad pr. lokation
            st.subheader("Udnyttelsesgrad pr. lokation")
            util_per_lokation = (
                daily_util.groupby("start_lokation", dropna=False)
                .agg(
                    total_hours=("Timer_total", "sum"),
                    total_hours_08_17=("Timer_08_17", "sum"),
                    total_vehicles=("license_plate", "nunique"),
            )
                .reset_index()
            )

            util_per_lokation["udnyttelse_pct_08_17"] = (util_per_lokation["total_hours_08_17"] / (num_days * WORKDAY_HOURS * util_per_lokation["total_vehicles"])).clip(upper=1.0) * 100.0

            # Søjlediagram: Udnyttelsesgrad pr. lokation (08-17)
            if not util_per_lokation.empty:
                fig_util_lok = px.bar(
                    util_per_lokation.sort_values("udnyttelse_pct_08_17", ascending=False),
                    x="start_lokation",
                    y="udnyttelse_pct_08_17",
                    title="Udnyttelsesgrad (08–17) pr. lokation",
                    labels={"start_lokation": "Lokation", "udnyttelse_pct_08_17": "Udnyttelsesgrad (08–17) (%)"},
                    text_auto=".1f",  # viser procenttal på søjlerne
                )

                fig_util_lok.update_layout(
                    xaxis_tickangle=-45,
                    yaxis_title="Udnyttelsesgrad (08–17) (%)",
                    xaxis_title="Lokation",
                    height=500,
                    margin=dict(l=40, r=40, t=60, b=100),
                )

                st.plotly_chart(fig_util_lok, use_container_width=True)
            else:
                st.info("Ingen data til at vise udnyttelsesgrad pr. lokation.")

            with st.expander("Se udnyttelsesgrad pr. lokation"):
                st.dataframe(
                util_per_lokation.rename(columns={
                    "start_lokation": "Lokation",
                    "total_vehicles": "Antal unikke biler",
                    "total_hours": "Total timer (alle dage)",
                    "total_hours_08_17": "Total timer (08-17)",
                    "udnyttelse_pct_08_17": "Udnyttelsesgrad (08-17) (%)",
                }),
                use_container_width=False,
            )



            st.subheader("Ledighed (kapacitet) over dagen")
            st.caption("Viser gennemsnitligt og minimalt antal ledige biler i halv-times intervaller mellem 00:00 og 23:30 for det valgte datointerval og filtre.")

            util_trips = filtered_util[filtered_util["end"] > filtered_util["start"]]
            fleet_size = vehicle_count
            dmin, dmax = pd.Timestamp(start_date), pd.Timestamp(end_date)
            half_hour = timedelta(minutes=30)
            slots = pd.date_range(dmin, periods=48, freq="30min").time

            # Alle kalenderdage i intervallet
            all_days = pd.date_range(dmin.normalize(), dmax.normalize(), freq="D")

            # Optælling af ledige pr. dag og slot
            rec = []
            for day in all_days:
                for tt in slots:
                    ws = datetime.datetime.combine(day.date(), tt)
                    we = ws + half_hour
                    # Aktiv hvis der er overlap mellem [start, end) og [ws, we)
                    active = util_trips.loc[(util_trips[COLS["start"]] < we) & (util_trips[COLS["end"]] > ws), COLS["reg"]].nunique()
                    ledige = max(0, fleet_size - int(active)) if fleet_size > 0 else 0
                    rec.append({
                        "slot": ws.strftime("%H:%M"),
                        "ledige": ledige,
                    })

            if rec:
                df_ledig = pd.DataFrame.from_records(rec)
                cap = (
                    df_ledig.groupby("slot", dropna=False)
                    .agg(
                        gennemsnit=("ledige", "mean"),
                        minimum=("ledige", "min")
                    )
                    .reset_index()
                    .sort_values("slot")
                )
                # Plot som grouped bars (som i eksemplet)
                fig_cap = px.bar(
                    cap,
                    x="slot",
                    y=["gennemsnit", "minimum"],
                    barmode="group",
                    labels={
                        "slot": "Tidspunkt",
                        "value": "Antal ledige biler",
                        "variable": "Metrik",
                    },
                    title="Ledighed (kapacitet) over dagen",
                    color_discrete_map={"gennemsnit": "#636EFA", "minimum": "#FFA15A"},
                )
                fig_cap.update_layout(legend_title_text="")
                st.plotly_chart(fig_cap, use_container_width=True)
                st.caption(f"Beregnet på {fleet_size} biler i udvalget og {len(all_days)} kalenderdage.")
            else:
                st.info("Ingen ture i perioden — kan ikke beregne ledighed.")

            # Udnyttelsesgrad pr. køretøj
            st.subheader("Udnyttelsesgrad pr. køretøj")

            util_per_vehicle = (
                daily_util.groupby("license_plate", dropna=False)
                .agg(
                    total_hours=("Timer_total", "sum"),
                    total_hours_08_17=("Timer_08_17", "sum"),
                    total_days=("Dato", "nunique"),
                )
                .reset_index()
            )

            util_per_vehicle["udnyttelse_pct_08_17"] = (util_per_vehicle["total_hours_08_17"] / (num_days * WORKDAY_HOURS)).clip(upper=1.0) * 100.0
            util_per_vehicle["udnyttelse_pct_16_timer"] = (util_per_vehicle["total_hours"] / (num_days * 16)).clip(upper=1.0) * 100.0
            util_per_vehicle["udnyttelse_pct_24_timer"] = (util_per_vehicle["total_hours"] / (num_days * 24)).clip(upper=1.0) * 100.0

            # Søjlediagram: Udnyttelsesgrad pr. køretøj (08–17)
            if not util_per_vehicle.empty:
                fig_util_vehicle = px.bar(
                    util_per_vehicle.sort_values("udnyttelse_pct_08_17", ascending=False),
                    x="license_plate",
                    y="udnyttelse_pct_08_17",
                    title="Udnyttelsesgrad (08–17) pr. køretøj",
                    labels={
                        "license_plate": "Køretøj",
                        "udnyttelse_pct_08_17": "Udnyttelsesgrad (08–17) (%)"
                    },
                    text_auto=".1f",
                )

                fig_util_vehicle.update_layout(
                    xaxis_tickangle=-45,
                    yaxis_title="Udnyttelsesgrad (08–17) (%)",
                    xaxis_title="Køretøj",
                    height=500,
                    margin=dict(l=40, r=40, t=60, b=100),
                )

                st.plotly_chart(fig_util_vehicle, use_container_width=True)
            else:
                st.info("Ingen data til at vise udnyttelsesgrad pr. køretøj.")


            with st.expander('Se udnyttelsesgrad pr. køretøj'):
                st.dataframe(
                    util_per_vehicle.rename(columns={
                        "license_plate": "Køretøj",
                        "total_hours": "Total timer (alle dage)",
                        "total_hours_08_17": "Total timer (08-17)",
                        "total_days": "Antal dage med kørsel",
                        "udnyttelse_pct_08_17": "Udnyttelsesgrad (08-17) (%)",
                        "udnyttelse_pct_16_timer": "Udnyttelsesgrad (16 timer) (%)",
                        "udnyttelse_pct_24_timer": "Udnyttelsesgrad (24 timer) (%)",
                    }),
                    use_container_width=False,
                )

            # Rå data (før aggregering)
            with st.expander("Se rå data (før aggregering)"):
                st.dataframe(
                    daily_util.rename(columns={
                        "Dato": "Dato",
                        "license_plate": "Køretøj",
                        "Timer_total": "Timer (total)",
                        "Timer_08_17": "Timer (08-17)",
                        "udnyttelse_pct_08_17": "Udnyttelsesgrad (08-17) (%)",
                    }),
                    use_container_width=False,
                )

    with tabs[3]:
        st.header("Ugedagsanvendelse")
        st.markdown(
            """
            Denne fane viser hvordan køretøjerne bliver brugt fordelt på
            ugedage. Du kan både se antal ture, gennemsnitlige ture pr.
            kalenderdag og hvordan varigheden og længden af turene udvikler
            sig hen over ugen.
            """
        )
        weekday_loc, weekday_overall = compute_weekday_metrics(
            filtered, start_date=start_date, end_date=end_date
        )
        if weekday_loc.empty:
            st.info("Ingen ture i den valgte periode/filtre.")
        else:
            weekday_loc = weekday_loc.sort_values(
                ["weekday_num", "start_lokation"], ascending=[True, True]
            )
            category_orders = {"weekday": WEEKDAY_ORDER}
            col1, col2 = st.columns(2)
            with col1:
                fig_trips = px.bar(
                    weekday_loc,
                    x="weekday",
                    y="trips",
                    color="start_lokation",
                    category_orders=category_orders,
                    labels={
                        "weekday": "Ugedag",
                        "trips": "Antal ture",
                        "start_lokation": "Lokation",
                    },
                    title="Antal ture pr. ugedag og lokation",
                )
                st.plotly_chart(fig_trips, use_container_width=True)
            with col2:
                fig_avg_trips = px.bar(
                    weekday_loc,
                    x="weekday",
                    y="avg_trips_per_day",
                    color="start_lokation",
                    category_orders=category_orders,
                    labels={
                        "weekday": "Ugedag",
                        "avg_trips_per_day": "Gns. ture pr. kalenderdag",
                        "start_lokation": "Lokation",
                    },
                    title="Gns. antal ture pr. ugedag (kalenderbaseret)",
                )
                fig_avg_trips.update_yaxes(tickformat=",.2f")
                st.plotly_chart(fig_avg_trips, use_container_width=True)
            distance_cols = {"total_distance": "Samlede km", "avg_distance": "Gns. km pr. tur"}
            available_distance = [c for c in distance_cols if c in weekday_loc.columns]
            if available_distance:
                fig_distance = px.bar(
                    weekday_loc,
                    x="weekday",
                    y=available_distance[0],
                    color="start_lokation",
                    category_orders=category_orders,
                    labels={
                        "weekday": "Ugedag",
                        available_distance[0]: distance_cols[available_distance[0]],
                        "start_lokation": "Lokation",
                    },
                    title=(
                        "Samlede kilometre pr. ugedag" if available_distance[0] == "total_distance"
                        else "Gennemsnitlig tur-længde pr. ugedag"
                    ),
                )
                st.plotly_chart(fig_distance, use_container_width=True)

            # Ny visualisering: Samlet varighed (timer) pr. ugedag og lokation
            if "total_duration" in weekday_loc.columns:
                fig_total_duration = px.bar(
                    weekday_loc,
                    x="weekday",
                    y="total_duration",
                    color="start_lokation",
                    category_orders=category_orders,
                    labels={
                        "weekday": "Ugedag",
                        "total_duration": "Samlet varighed (timer)",
                        "start_lokation": "Lokation",
                    },
                    title="Samlet varighed pr. ugedag og lokation",
                )
                fig_total_duration.update_yaxes(tickformat=",.2f")
                st.plotly_chart(fig_total_duration, use_container_width=True)
            with st.expander("Detaljeret tabel pr. lokation og ugedag"):
                display_cols = {
                    "start_lokation": "Lokation",
                    "weekday": "Ugedag",
                    "trips": "Ture",
                    "avg_trips_per_day": "Gns. ture pr. kalenderdag",
                    "total_distance": "Samlede km",
                    "avg_distance": "Gns. km pr. tur",
                    "total_duration": "Samlet varighed (timer)",
                    "avg_duration": "Gns. varighed (timer)",
                    "days_in_range": "Antal dage i intervallet",
                }
                cols_to_show = [c for c in display_cols if c in weekday_loc.columns]
                st.dataframe(
                    weekday_loc[cols_to_show]
                    .rename(columns={k: v for k, v in display_cols.items() if k in cols_to_show})
                    .reset_index(drop=True),
                    use_container_width=True,
                )
        if not weekday_overall.empty:
            weekday_overall = weekday_overall.sort_values("weekday_num")
            if "avg_duration" in weekday_overall.columns:
                duration_df = weekday_overall[["weekday", "avg_duration", "median_duration"]].copy()
                duration_df["avg_duration_min"] = duration_df["avg_duration"] * 60
                duration_df["median_duration_min"] = duration_df["median_duration"] * 60
                fig_duration = px.bar(
                    duration_df,
                    x="weekday",
                    y="avg_duration_min",
                    category_orders={"weekday": WEEKDAY_ORDER},
                    labels={
                        "weekday": "Ugedag",
                        "avg_duration_min": "Gns. varighed (minutter)",
                    },
                    title="Gennemsnitlig varighed pr. tur fordelt på ugedage",
                )
                fig_duration.update_traces(marker_color="#636EFA")
                st.plotly_chart(fig_duration, use_container_width=True)
                st.caption(
                    "Medianen (vist i tabellen herunder) giver et robust billede af de typiske ture."
                )
            if "avg_distance" in weekday_overall.columns:
                distance_overall = weekday_overall[["weekday", "avg_distance", "median_distance"]].copy()
                fig_avg_distance = px.line(
                    distance_overall,
                    x="weekday",
                    y="avg_distance",
                    category_orders={"weekday": WEEKDAY_ORDER},
                    markers=True,
                    labels={
                        "weekday": "Ugedag",
                        "avg_distance": "Gns. km pr. tur",
                    },
                    title="Gennemsnitlig turlængde på tværs af alle lokationer",
                )
                st.plotly_chart(fig_avg_distance, use_container_width=True)
            with st.expander("Samlet oversigt pr. ugedag"):
                overall_display = {
                    "weekday": "Ugedag",
                    "trips": "Ture",
                    "avg_trips_per_day": "Gns. ture pr. kalenderdag",
                    "avg_duration": "Gns. varighed (timer)",
                    "median_duration": "Median varighed (timer)",
                    "avg_distance": "Gns. km pr. tur",
                    "median_distance": "Median km pr. tur",
                    "days_in_range": "Antal dage i intervallet",
                }
                cols_to_show = [c for c in overall_display if c in weekday_overall.columns]
                st.dataframe(
                    weekday_overall[cols_to_show]
                    .rename(columns={k: v for k, v in overall_display.items() if k in cols_to_show})
                    .reset_index(drop=True),
                    use_container_width=True,
                )

        # Per-bil oversigt under samme fane
        st.subheader("Per bil")
        st.markdown(
            "Se hvilke biler der anvendes pr. ugedag på de valgte lokationer."
        )

        wk = filtered.copy()
        wk["date"] = pd.to_datetime(wk["date"], errors="coerce")
        wk = wk.dropna(subset=["date", "start_lokation", "license_plate"]).copy()
        wk["weekday_num"] = wk["date"].dt.weekday
        wk["weekday"] = wk["weekday_num"].map(WEEKDAY_LABELS_DA)

        # Valgfrit ekstra filter for lokation
        loc_options = sorted(wk["start_lokation"].dropna().unique().tolist())
        loc_sel = st.multiselect(
            "Filtrer lokationer (valgfrit)",
            options=loc_options,
            default=selected_lokations or []
        )
        if loc_sel:
            wk = wk[wk["start_lokation"].isin(loc_sel)]

        # Aggreger per lokation, ugedag og bil
        agg_kwargs = {"trips": ("license_plate", "count")}
        if "distance_km" in wk.columns:
            agg_kwargs["km"] = ("distance_km", "sum")
        if "duration_hours" in wk.columns:
            agg_kwargs["timer"] = ("duration_hours", "sum")

        per_vehicle = (
            wk.groupby(["start_lokation", "weekday_num", "weekday", "license_plate"], dropna=False)
              .agg(**agg_kwargs)
              .reset_index()
              .sort_values(["start_lokation", "weekday_num", "license_plate"])
        )

        # Unikke biler og sum ture pr. ugedag og lokation
        vehicles_per_weekday = (
            per_vehicle.groupby(["start_lokation", "weekday", "weekday_num"], dropna=False)
              .agg(unique_vehicles=("license_plate", "nunique"),
                   trips=("trips", "sum"))
              .reset_index()
              .sort_values(["weekday_num", "start_lokation"])
        )

        c1, c2 = st.columns(2)
        with c1:
            fig_uv = px.bar(
                vehicles_per_weekday,
                x="weekday",
                y="unique_vehicles",
                color="start_lokation",
                category_orders={"weekday": WEEKDAY_ORDER},
                labels={"weekday": "Ugedag", "unique_vehicles": "Unikke biler", "start_lokation": "Lokation"},
                title="Unikke biler pr. ugedag og lokation",
            )
            st.plotly_chart(fig_uv, use_container_width=True)

        with c2:
            fig_trips2 = px.bar(
                vehicles_per_weekday,
                x="weekday",
                y="trips",
                color="start_lokation",
                category_orders={"weekday": WEEKDAY_ORDER},
                labels={"weekday": "Ugedag", "trips": "Ture", "start_lokation": "Lokation"},
                title="Ture pr. ugedag og lokation",
            )
            st.plotly_chart(fig_trips2, use_container_width=True)

        # Stacked charts per vehicle (legend can toggle vehicles)
        if not per_vehicle.empty:
            # Days in range per weekday for averaging
            start = pd.to_datetime(start_date) if start_date else wk["date"].min()
            end = pd.to_datetime(end_date) if end_date else wk["date"].max()
            if pd.isna(start) or pd.isna(end) or start > end:
                weekday_counts = pd.DataFrame({"weekday_num": range(7), "days_in_range": 0})
            else:
                dr = pd.date_range(start, end, freq="D")
                weekday_counts = (
                    pd.Series(dr.weekday)
                    .value_counts()
                    .reindex(range(7), fill_value=0)
                    .rename("days_in_range")
                    .rename_axis("weekday_num")
                    .reset_index()
                )

            per_vehicle_avg = per_vehicle.merge(weekday_counts, on="weekday_num", how="left")
            per_vehicle_avg["days_in_range"] = per_vehicle_avg["days_in_range"].fillna(0)
            per_vehicle_avg["avg_trips_per_day"] = np.where(
                per_vehicle_avg["days_in_range"] > 0,
                per_vehicle_avg["trips"] / per_vehicle_avg["days_in_range"],
                np.nan,
            )
            if "timer" in per_vehicle_avg.columns:
                per_vehicle_avg["avg_timer_per_day"] = np.where(
                    per_vehicle_avg["days_in_range"] > 0,
                    per_vehicle_avg["timer"] / per_vehicle_avg["days_in_range"],
                    np.nan,
                )

            st.markdown("—")
            st.subheader("Stablede søjler pr. ugedag (vælg biler via legend)")
            c3, c4 = st.columns(2)
            with c3:
                if "timer" in per_vehicle.columns:
                    fig_tot_dur = px.bar(
                        per_vehicle,
                        x="weekday",
                        y="timer",
                        color="license_plate",
                        category_orders={"weekday": WEEKDAY_ORDER},
                        labels={
                            "weekday": "Ugedag",
                            "timer": "Samlet varighed (timer)",
                            "license_plate": "Bil",
                        },
                        title="Samlet varighed pr. ugedag (stablet)",
                    )
                    fig_tot_dur.update_layout(barmode="stack")
                    fig_tot_dur.update_yaxes(tickformat=",.2f")
                    st.plotly_chart(fig_tot_dur, use_container_width=True)
                else:
                    st.info("Ingen varighedskolonne i data (duration_hours mangler).")
            with c4:
                fig_trips_stacked = px.bar(
                    per_vehicle,
                    x="weekday",
                    y="trips",
                    color="license_plate",
                    category_orders={"weekday": WEEKDAY_ORDER},
                    labels={
                        "weekday": "Ugedag",
                        "trips": "Antal ture",
                        "license_plate": "Bil",
                    },
                    title="Antal ture pr. ugedag (stablet)",
                )
                fig_trips_stacked.update_layout(barmode="stack")
                st.plotly_chart(fig_trips_stacked, use_container_width=True)

            c5, c6 = st.columns(2)
            with c5:
                fig_avg_trips_stacked = px.bar(
                    per_vehicle_avg,
                    x="weekday",
                    y="avg_trips_per_day",
                    color="license_plate",
                    category_orders={"weekday": WEEKDAY_ORDER},
                    labels={
                        "weekday": "Ugedag",
                        "avg_trips_per_day": "Gns. ture pr. kalenderdag",
                        "license_plate": "Bil",
                    },
                    title="Gns. ture pr. ugedag (stablet)",
                )
                fig_avg_trips_stacked.update_layout(barmode="stack")
                fig_avg_trips_stacked.update_yaxes(tickformat=",.2f")
                st.plotly_chart(fig_avg_trips_stacked, use_container_width=True)
            with c6:
                if "avg_timer_per_day" in per_vehicle_avg.columns:
                    fig_avg_dur_stacked = px.bar(
                        per_vehicle_avg,
                        x="weekday",
                        y="avg_timer_per_day",
                        color="license_plate",
                        category_orders={"weekday": WEEKDAY_ORDER},
                        labels={
                            "weekday": "Ugedag",
                            "avg_timer_per_day": "Gns. varighed pr. dag (timer)",
                            "license_plate": "Bil",
                        },
                        title="Gns. varighed pr. ugedag (stablet)",
                    )
                    fig_avg_dur_stacked.update_layout(barmode="stack")
                    fig_avg_dur_stacked.update_yaxes(tickformat=",.2f")
                    st.plotly_chart(fig_avg_dur_stacked, use_container_width=True)
                else:
                    st.info("Ingen varighedskolonne i data (duration_hours mangler).")

        # Valgfri heatmap – når præcis én lokation er valgt
        if loc_sel and len(loc_sel) == 1 and not per_vehicle.empty:
            top_sel = st.slider("Vis top N biler (efter ture)", min_value=5, max_value=50, value=20, step=5)
            one_loc = per_vehicle[per_vehicle["start_lokation"] == loc_sel[0]].copy()
            top_vehicles = (
                one_loc.groupby("license_plate")["trips"].sum()
                       .sort_values(ascending=False)
                       .head(top_sel).index.tolist()
            )
            one_loc = one_loc[one_loc["license_plate"].isin(top_vehicles)]
            if not one_loc.empty:
                fig_heat = px.density_heatmap(
                    one_loc,
                    x="weekday",
                    y="license_plate",
                    z="trips",
                    category_orders={"weekday": WEEKDAY_ORDER},
                    labels={"weekday": "Ugedag", "license_plate": "Bil", "trips": "Ture"},
                    title=f"Aktivitet pr. bil og ugedag – {loc_sel[0]}",
                )
                st.plotly_chart(fig_heat, use_container_width=True)

        with st.expander("Detaljer pr. lokation, ugedag og bil"):
            rename_map = {
                "start_lokation": "Lokation",
                "weekday": "Ugedag",
                "license_plate": "Bil",
                "trips": "Ture",
                "km": "Km",
                "timer": "Timer",
            }
            cols = [c for c in ["start_lokation", "weekday", "license_plate", "trips", "km", "timer"] if c in per_vehicle.columns]
            st.dataframe(
                per_vehicle[cols].rename(columns=rename_map),
                use_container_width=True,
                hide_index=True,
            )




if __name__ == "__main__":
    main()
