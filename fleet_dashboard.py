"""
Fleet Dashboard
===============

This Streamlit application loads fleet data from the provided Excel files,
performs the calculations outlined in the analysis plan (e.g. utilisation
per location and vehicle type, private versus municipal usage, employee
private car usage, and daily utilisation), and displays the results in an
interactive dashboard.  The dashboard is divided into several tabs with
filters to allow the user (e.g. the Fleet Manager) to explore the data.

Running the dashboard
---------------------

Before running this script you must install the required Python packages:

.. code-block:: shell

   pip install streamlit pandas plotly openpyxl

Once the dependencies are installed, you can start the dashboard with:

.. code-block:: shell

   streamlit run fleet_dashboard.py

The dashboard will open in your web browser.  Since the Fleet Manager (FM)
cannot install Python on his machine, you can run the dashboard yourself
and share the results by exporting screenshots or by allowing FM to
connect to the streamlit app over the network if possible.

Note
----

The column names used in the data cleaning section are based on the
provided analysis material and may need adjustment to match your actual
Excel files.  If the column names differ, please update the
``COLUMN_MAPPING`` dictionary accordingly.
"""

import datetime
from typing import List, Optional


import pandas as pd
import numpy as np
import plotly.express as px
import streamlit as st



# -----------------------------------------------------------------------------
# Configuration
#
# Modify this dictionary if your column names differ from the expected ones.
# Keys are the expected internal names used in this script; values are the
# column names as they appear in your ``Kørebog_fiktiv_raw.xlsx`` file.
#
# ``date``: the date of the trip (as datetime or string)
# ``start_time``: the start time of the trip (as string or datetime)
# ``end_time``: the end time of the trip (as string or datetime)
# ``department``: the location/department identifier from the trip record
# ``employee``: the person who performed the trip
# ``license_plate``: the vehicle registration or identifier used in the trip
# ``distance_km``: the distance of the trip in kilometres
# ``drivmiddel``: textual description of the drive type (e.g. "El", "Diesel", etc.)



# Column mapping for the OPUS file (e.g. ``sidste_to_måneder.xlsx``).
# The OPUS data represents trips driven in private cars (own vehicle).
# Adjust the values in this dictionary to match the actual column names
# in your ``sidste_to_måneder.xlsx`` file.  Common columns include:
#
# - Dato: Date of the trip
# - Afdeling: Location/department
# - Medarbejder: Name or identifier of the employee
# - Antal km: Distance in kilometres
# - Tid: Duration in hours (optional; may not exist)
# - Registreringsnummer: Registration number (optional)
#




# Working day hours used for utilisation calculations (8:00 to 1:00 => 8 hours)
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


from datetime import datetime, time, timedelta


def most_frequent(series: pd.Series) -> str:
    if series.empty:
        return None
    return series.value_counts().idxmax()

def compute_home_locations(
    df_trips: pd.DataFrame,
    df_vehicles: pd.DataFrame | None = None,
    col_vehicle="license_plate",
    col_from_addr="start_lokation",
    col_home_in_vehicles="start_lokation",         # justér hvis din køretøjstabel hedder noget andet
    col_is_start_from_home="start_lokation"       # boolean i trips (hvis tilgængelig)
) -> pd.DataFrame:
    """
    Returnerer DataFrame med kolonner: [Registreringsnummer, Home location]
    1) Hvis df_vehicles har en kolonne for hjem-lokation, bruges den.
    2) Ellers udledes hjem-lokation som mest hyppige startadresse pr. køretøj.
       Hvis der findes en boolean 'start_lokation', bruges kun rækker hvor den er True.
    """
    # Case 1: direkte fra køretøjstabellen
    if df_vehicles is not None and col_home_in_vehicles in df_vehicles.columns:
        out = (df_vehicles[[col_vehicle, col_home_in_vehicles]]
               .dropna()
               .rename(columns={col_home_in_vehicles: "Home location"})
               .drop_duplicates())
        return out

    # Case 2: udled fra trips
    df_use = df_trips.copy()
    if col_is_start_from_home in df_use.columns:
        df_use = df_use[df_use[col_is_start_from_home] == True]

    if col_vehicle not in df_use.columns or col_from_addr not in df_use.columns:
        # Fald tilbage: tomt resultat
        return pd.DataFrame(columns=[col_vehicle, "Home location"]).astype({col_vehicle: "string", "Home location": "string"})

    out = (df_use
           .groupby(col_vehicle, dropna=True)[col_from_addr]
           .apply(most_frequent)
           .reset_index(name="Home location"))
    return out

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


def compute_overview_metrics(df: pd.DataFrame, num_workdays) -> pd.DataFrame:
    """Compute summary metrics per department and private.

    Returns a DataFrame with the following columns:

    * department
    * vehicels_type: vehicle type category
    * trips: total number of trips in the period
    * total_km: total kilometres driven in the period
    * total_duration: total duration in hours
    * unique_vehicles: number of distinct vehicles
    * avg_trips_per_day: average trips per day
    * avg_km_per_day: average kilometres per day
    * utilisation: total_duration / (unique_vehicles * number_of_days * WORKDAY_HOURS)
    """
    # Determine number of days in dataset
    # Sørg for at 'date' er datetime
    df["date"] = pd.to_datetime(df["date"], errors="coerce")

    grouped = (
        df.groupby(["start_lokation", "vehicels_type"], dropna=False)
        .agg(
            trips=("license_plate", "count"),
            total_km=("distance_km", "sum"),
            total_duration=("duration_hours", "sum"),
            unique_vehicles=("license_plate", "nunique"),
        )
        .reset_index()
    )
    grouped["avg_trips_per_day"] = grouped["trips"] / num_workdays
    grouped["avg_km_per_day"] = grouped["total_km"] / num_workdays
    grouped["num_workdays"] = num_workdays  # for reference/debugging
    return grouped


def compute_private_vs_municipal(df: pd.DataFrame) -> pd.DataFrame:
    """Calculate number of trips and kilometres for private vs municipal cars.

    Returns a DataFrame indexed by date, department and a boolean ``private``.
    """
    df_personbil = df[df["vehicels_type"] == "alm. personbil (≤ 5 personer)"].copy()

    grouped = (
        df_personbil.groupby(["date", "start_lokation", "private"], dropna=False)
        .agg(
            trips=("license_plate", "count"),
            km=("distance_km", "sum"),
            duration_hours=("duration_hours", "sum"),
        )
        .reset_index()
    )
    return grouped


def compute_employee_private_usage(df: pd.DataFrame) -> pd.DataFrame:
    """Compute total kilometres and trips per employee for private car usage."""
    df.dropna(subset='private', inplace=True)
    
    private_df = df[df["private"]].copy()
    grouped = (
        private_df.groupby("employee", dropna=False)
        .agg(
            trips=("license_plate", "count"),
            km=("distance_km", "sum"),
            duration_hours=("duration_hours", "sum"),
        )
        .reset_index()
        .sort_values("km", ascending=False)
    )
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
            ws = datetime.combine(cur.date(), WORKDAY_START)
            we = datetime.combine(cur.date(), WORKDAY_END)
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
    kilde: Optional[List[str]] = None,
    employees: Optional[List[str]] = None,
    vehicles_type: Optional[List[str]] = None,
    start_date: Optional[datetime.date] = None,
    end_date: Optional[datetime.date] = None,
) -> pd.DataFrame:
    """Apply filters to the merged dataset.

    Parameters
    ----------
    df : pandas.DataFrame
        The merged trip and vehicle data.
    lokationer : list of str, optional
        lokationer to include.  If ``None`` or empty, all departments are
        included.
    vehicles : list of str, optional
        Specific vehicles (license plates) to include.
    employees : list of str, optional
        Specific employees to include.
    start_date : datetime.date, optional
        Start of date range filter (inclusive).
    end_date : datetime.date, optional
        End of date range filter (inclusive).

    Returns
    -------
    pandas.DataFrame
        The filtered dataset.
    """
    mask = pd.Series(True, index=df.index)

    if lokationer:
        mask &= df["start_lokation"].isin(lokationer)

    if vehicles:
        mask &= df["license_plate"].isin(vehicles)

    if kilde:
        mask &= df["kilde"].isin(kilde)
    
    if employees:
        mask &= df["employee"].isin(employees)

    if vehicles_type:
        mask &= df["vehicels_type"].isin(vehicles_type)

    if start_date:
        mask &= df["date"] >= start_date

    if end_date:
        mask &= df["date"] <= end_date

    return df[mask].copy()


def main():
    """Main entry point for the Streamlit app."""
    st.set_page_config(
        page_title="Flådestyringsdashboard",
        layout="wide",
        initial_sidebar_state="expanded",
    )

    st.title("Flådestyringsdashboard")
    st.markdown(
        """
        Dette dashboard giver et overblik over flådens anvendelse baseret på data
        fra kørebogen og køretøjsregistret.  Du kan filtrere på lokationer,
        køretøjstyper, individuelle biler, medarbejdere og datointerval via
        sidebaren.
        """
    )

    data = None
    # --- Session state til at huske upload-status ---
    if "data" not in st.session_state:
        st.session_state["data"] = None
    if "changes" not in st.session_state:
        st.session_state["changes"] = None

    # --- Hvis datafilen ikke er uploadet ---
    if st.session_state["data"] is None:
        st.sidebar.info("Upload din datafil (påkrævet) for at starte analysen.")

        uploaded_data = st.sidebar.file_uploader(
            "Upload datafil (CSV) – påkrævet",
            type=["csv"],
            key="data_uploader"
        )

        uploaded_changes = st.sidebar.file_uploader(
            "Upload ændringsfil (Excel) – valgfrit",
            type=["xlsx"],
            key="changes_uploader"
        )

        # Gem datafil, når den uploades
        if uploaded_data is not None:
            st.session_state["data"] = pd.read_csv(uploaded_data)

        # Gem ændringsfil, hvis uploadet
        if uploaded_changes is not None:
            st.session_state["changes"] = pd.read_excel(uploaded_changes)
            st.info("Ændringsfil indlæst (valgfri).")
    else:
        # --- Når datafil er uploadet ---
        st.sidebar.info("Datafil er indlæst.")
        if st.sidebar.button("Upload ny datafil"):
            st.session_state["data"] = None
            st.session_state["changes"] = None
            st.rerun()

    # --- Hovedindhold ---
    if st.session_state["data"] is not None:
        data = st.session_state["data"]

        # Ændringsfil kun hvis den findes
        if st.session_state["changes"] is not None:
            df_changes_adresses = st.session_state["changes"]
            dict_fra_excel = dict(zip(df_changes_adresses.iloc[:, 0], df_changes_adresses.iloc[:, 1]))
            data['start_lokation'] = data['start_lokation'].replace(dict_fra_excel)
            data['end_lokation'] = data['end_lokation'].replace(dict_fra_excel)
    else:
        st.warning("Upload en datafil i sidepanelet for at fortsætte.")

    if data is not None:
        # Build lists for filters
        all_lokations = sorted(
            [x for x in data["start_lokation"].dropna().unique().tolist() if x]
        )

        all_kilder = sorted(
            [x for x in data["kilde"].dropna().unique().tolist() if x]
        )

        all_vehicles = sorted(
            [x for x in data["license_plate"].dropna().unique().tolist() if x]
        )
        all_employees = sorted(
            [x for x in data["employee"].dropna().unique().tolist() if x]
        )
        
        all_vehicles_types = sorted(
            [x for x in data["vehicels_type"].dropna().unique().tolist() if x]
        )

        # Sidebar filters
        st.sidebar.header("Filtre")
        selected_lokations = st.sidebar.multiselect(
            "Vælg lokationer", options=all_lokations, default=[]
        )
        
        selected_kilder = st.sidebar.multiselect(
            "Vælg kilde", options=all_kilder, default=[]
        )

        selected_vehicles = st.sidebar.multiselect(
            "Vælg biler", options=all_vehicles, default=[]
        )
        selected_employees = st.sidebar.multiselect(
            "Vælg medarbejdere", options=all_employees, default=[]
        )

        selected_vehicles_type = st.sidebar.multiselect(
            "Vælg køretøjstype", options=all_vehicles_types, default=[]
        )


        
        data['date'] = pd.to_datetime(data['date'], errors='coerce')
        num_workdays = data.loc[data["date"].dt.weekday < 5, "date"].nunique()

        # Konverter og filtrer datoer
        data['date'] = pd.to_datetime(data['date'], errors='coerce').dt.date
        data = data.dropna(subset=['date'])

        # Date range filter
        min_date = data["date"].min()
        max_date = data["date"].max()

        
        date_range = st.sidebar.date_input(
            "Dato-interval", value=(min_date, max_date), min_value=min_date, max_value=max_date
        )
        start_date, end_date = None, None
        if isinstance(date_range, tuple) and len(date_range) == 2:
            start_date, end_date = date_range
        elif isinstance(date_range, datetime.date):
            start_date = end_date = date_range

        # Filter data
        filtered = filter_data(
            data,
            lokationer=selected_lokations or None,
            vehicles=selected_vehicles or None,
            kilde=selected_kilder or None,
            employees=selected_employees or None,
            vehicles_type=selected_vehicles_type or None,
            start_date=start_date,
            end_date=end_date,
        )

        # Compute metrics for filtered data
        overview = compute_overview_metrics(filtered,num_workdays)
        private_vs = compute_private_vs_municipal(filtered)
        emp_private = compute_employee_private_usage(filtered)
        daily_util = compute_daily_utilization(filtered)

        NUMBERS_OF_VEHICELS = filtered["license_plate"].nunique()

        # Tabs for different analyses
        tabs = st.tabs(
            [
                "Oversigt",
                "Lokationer & køretøjstyper",
                "Privat vs kommunal",
                "Egen bil pr. medarbejder",
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
            NUMBERS_OF_VEHICELS_KOMMUNE = filtered[filtered["kilde"] == "Kørebog"]["license_plate"].nunique()
            gns_udnyttelse = total_time / (NUMBERS_OF_VEHICELS_KOMMUNE * num_workdays * WORKDAY_HOURS) * 100.0 if NUMBERS_OF_VEHICELS_KOMMUNE > 0 and num_workdays > 0 else 0.0

            c1, c2, c3, c4, c5 = st.columns(5)
            c1.metric("Antal køretøjer", f"{unique_vehicles:,}")
            c2.metric("Lokationer", f"{unique_locations:,}")
            c3.metric("Samlede km", f"{total_km:,.0f}".replace(",", "."))
            c4.metric("Ture", f"{total_trips:,.0f}".replace(",", "."))
            c5.metric("Gns. udnyttelse pr. dag(8–17)", f"{gns_udnyttelse:.1f}%")

            # Display metrics as table

            st.subheader("")
            st.markdown(
                """
                I tabellen nedenfor vises antallet af køretøjer i køretøjstype.
                """
            )
            c1,c2 = st.columns(2)
            with c1:
                st.markdown("**Antal antallet køretøjer pr. køretøjstype**")
                veh_counts = (
                    filtered.groupby("vehicels_type", dropna=False)
                    .agg(unique_vehicles=("license_plate", "nunique"))
                    .reset_index()
                    .sort_values("unique_vehicles", ascending=False)
                )
                st.dataframe(
                    veh_counts.rename(
                        columns={
                            "vehicels_type": "Køretøjstype",
                            "unique_vehicles": "Antal unikke køretøjer",
                        }
                    ),
                    use_container_width=True,
                    hide_index=True,
                )
            with c2:
                st.markdown("**Ikke kategoriceret køretøjstype**")
                missing_veh_type = filtered[filtered["vehicels_type"].isna()]
                missing_veh_type = missing_veh_type[["license_plate", "start_lokation", "kilde"]].drop_duplicates().sort_values("license_plate")  
                st.dataframe(missing_veh_type, use_container_width=True)
            
            # Chart: average trips per day by location and vehicle type
            if not overview.empty:
                fig1 = px.bar(
                    overview,
                    x="start_lokation",
                    y="avg_trips_per_day",
                    color="vehicels_type",
                    barmode="group",
                    labels={
                        "start_lokation": "Lokation",
                        "avg_trips_per_day": "Gennemsnitlige ture pr. dag",
                        "vehicel_type": "Køretøjstype",
                    },
                    title="Gennemsnitlige ture pr. dag pr. lokation og køretøjstype",
                )
                st.plotly_chart(fig1, use_container_width=True)

        # Location & vehicle type tab
        with tabs[1]:
            st.header("Lokationer & køretøj")

            st.markdown("Nøgletal pr. lokation og køretøjstype.")
            st.dataframe(overview, use_container_width=True)
            # Chart: utilisation by location and vehicle type
            if not overview.empty:
                fig2 = px.bar(
                    overview,
                    x="start_lokation",
                    y="unique_vehicles",
                    color="vehicels_type",
                    barmode="group",
                    labels={
                        "start_lokation": "Lokation",
                        "unique_vehicles": "Antal køretøjer",
                        "vehicels_type": "Køretøjstype",
                    },
                    title="Antal køretøjer pr. lokation og køretøjstype",
                )
                st.plotly_chart(fig2, use_container_width=True)

            st.subheader("Hvor køretøjerne hører hjemme")

            # Forudsætninger (justér disse tre variabler så de peger på dine allerede filtrerede dataframes):
            # df_trips_filtered: dit nuværende trips-df efter brugerens filtre i dashboardet
            # df_vehicles_filtered: (valgfrit) køretøjs-oversigt efter samme filtre (hvis du har en sådan)
            # Hvis du ikke har en separat køretøjs-DF, sæt df_vehicles_filtered = None

            home_df = compute_home_locations(
                df_trips=filtered,
                df_vehicles=filtered,  # eller None
                col_vehicle="license_plate",
                col_from_addr="start_lokation",
                col_home_in_vehicles="start_lokation",      # ret hvis din kolonne hedder noget andet
                col_is_start_from_home="start_lokation"    # ret/lad blive hvis den findes i dine trips
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
            with st.expander("Se tabel: Køretøj → Home location"):
                st.dataframe(
                    home_df.rename(columns={
                        "license_plate": "Køretøj",
                        "Home location": "Home location"
                    }).sort_values("Køretøj"),
                    use_container_width=True
                )


        # Private vs municipal tab
        with tabs[2]:
            st.header("Privat vs kommunal")
            st.markdown(
                "Denne fane viser, hvor mange ture og kilometre der er kørt i private biler" \
                "sammenlignet med kommunale personbiler pr. dag og lokation."
            )
            # Aggregate totals across all days for display
            agg_private = private_vs.groupby(["start_lokation", "private"]).agg(
                trips=("trips", "sum"), km=("km", "sum")
            ).reset_index()
            # Replace boolean with string for readability
            agg_private["Biltype"] = agg_private["private"].map(
                {True: "Privat", False: "Kommunal"}
            )


            st.dataframe(
                agg_private.rename(columns={"start_lokation": "Lokation", "trips" : "Ture"})[
                    ["Lokation", "Biltype", "Ture", "km"]
                ],
                use_container_width=True, hide_index=True
            )


            # Pie charts summarising private vs municipal usage
            col1, col2 = st.columns(2, gap="medium")

            color_map = {"Privat": "#636EFA", "Kommunal": "#EF553B"}
            category_order = {"Biltype": ["Privat", "Kommunal"]}

            total_trips = agg_private["trips"].sum()
            if total_trips > 0:
                fig3 = px.pie(
                    agg_private,
                    names="Biltype",
                    values="trips",
                    title="Andel af ture: privat vs kommunal",
                    color="Biltype",
                    color_discrete_map=color_map,
                    category_orders=category_order,
                )
                with col1:
                    st.plotly_chart(fig3, use_container_width=True)

            total_km = agg_private["km"].sum()
            if total_km > 0:
                fig4 = px.pie(
                    agg_private,
                    names="Biltype",
                    values="km",
                    title="Andel af kilometre: privat vs kommunal",
                    color="Biltype",
                    color_discrete_map=color_map,
                    category_orders=category_order,
                )
                with col2:
                    st.plotly_chart(fig4, use_container_width=True)

            # 100%-stablet søjlediagram pr. lokation
            if not agg_private.empty:
                # Fjern lokationer med kun 1 tur (uanset biltype)
                trips_per_loc = agg_private.groupby("start_lokation")["trips"].sum()
                valid_locs = trips_per_loc[trips_per_loc > 1].index
                agg_filtered = agg_private[agg_private["start_lokation"].isin(valid_locs)]

                if not agg_filtered.empty:
                    # Beregn andel pr. lokation
                    pct_df = (
                        agg_filtered.groupby("start_lokation")
                        .apply(lambda g: g.assign(
                            andel=g["trips"] / g["trips"].sum() * 100
                        ))
                        .reset_index(drop=True)
                    )

                    # Tilføj tekstlabel med både procent og antal ture
                    pct_df["label"] = pct_df.apply(
                        lambda r: f"{r['andel']:.1f}% ({int(r['trips'])} ture)", axis=1
                    )

                    pct_df.sort_values(by=["start_lokation"], inplace=True)

                    fig_pct = px.bar(
                        pct_df,
                        x="start_lokation",
                        y="andel",
                        color="Biltype",
                        barmode="stack",
                        text="label",  # bruger den nye labelkolonne
                        color_discrete_map=color_map,
                        category_orders=category_order,
                        labels={
                            "start_lokation": "Lokation",
                            "andel": "Andel af ture (%)",
                            "Biltype": "Biltype"
                        },
                        title="Andel af ture (Privat vs Kommunal) pr. lokation – 100 % stablet",
                    )

                    fig_pct.update_traces(textposition="inside", textfont_size=11)
                    fig_pct.update_layout(yaxis=dict(range=[0, 100]))
                    st.plotly_chart(fig_pct, use_container_width=True)
                else:
                    st.info("Ingen lokationer med mere end 1 tur at vise.")




        # Employee private usage tab
        with tabs[3]:
            st.header("Egen bil pr. medarbejder")
            st.markdown("Total kørsel i egen bil pr. medarbejder i den valgte periode.")
            if emp_private.empty:
                st.info("Ingen registrerede ture i privat bil for de valgte filtre.")
            else:
                st.dataframe(
                    emp_private.rename(columns={
                        "employee": "Medarbejder",
                        "trips": "Ture",
                        "km": "Kilometre",
                        "duration_hours": "Timer",
                    }),
                    use_container_width=True,
                )
                fig5 = px.bar(
                    emp_private,
                    x="employee",
                    y="km",
                    labels={"employee": "Medarbejder", "km": "Kilometre"},
                    title="Kilometre i egen bil pr. medarbejder",
                )
                st.plotly_chart(fig5, use_container_width=True)

        # Utilisation over time tab
        with tabs[4]:
            st.header("Udnyttelsesgrad over tid")
            st.markdown(
                "Gennemsnitlig udnyttelsesgrad pr. dag på tværs af alle valgte biler og lokationer."
            )
            if daily_util.empty:
                st.info("Ingen data til rådighed for de valgte filtre.")
            else:
                
                # Lokalt filter for denne side
                st.markdown("**Filtrering på biltype (kun for denne visning)**")

                available_types = sorted([x for x in filtered["vehicels_type"].dropna().unique()])
                selected_types = st.multiselect(
                    "Vælg køretøjstype(r)",
                    options=available_types,
                    default=available_types,
                    key="vehicletype_filter_utilisation"
                )

                # Filtrer data kun for denne side
                filtered_util = filtered[filtered["vehicels_type"].isin(selected_types)].copy()
                
                daily_util = compute_daily_utilization(filtered_util)

                NUMBERS_OF_VEHICELS = daily_util['license_plate'].nunique()
                
                # 1) Læs interval fra UI
                selected_min, selected_max = date_range  # fra din st.sidebar.date_input

                # 2) Sørg for, at "Dato" er datetime64 og normaliseret til dato (uden tid)
                daily_util["Dato"] = pd.to_datetime(daily_util["Dato"]).dt.normalize()

                # 3) Filtrér data til valgt interval
                mask = (daily_util["Dato"] >= pd.to_datetime(selected_min)) & (daily_util["Dato"] <= pd.to_datetime(selected_max))
                daily_util_f = daily_util.loc[mask].copy()

                # 4) Aggreger timer pr. dag
                daily_util_agg = (
                    daily_util_f.groupby("Dato", dropna=False)
                    .agg(
                        hours_total=("Timer_total", "sum"),
                        hours_08_17=("Timer_08_17", "sum"),
                    )
                    .reset_index()
                )

                # 5) Lav en komplet kalender med alle datoer i intervallet
                all_dates = pd.DataFrame({"Dato": pd.date_range(pd.to_datetime(selected_min), pd.to_datetime(selected_max), freq="D")})

                # 6) Merge for at sikre alle datoer er med, og udfyld manglende med 0
                daily_util_agg = (
                    all_dates.merge(daily_util_agg, on="Dato", how="left")
                    .fillna({"hours_total": 0.0, "hours_08_17": 0.0})
                )

                # 7) Beregn gennemsnit pr. køretøj (global variabel)
                #    OBS: Tjek stavning – du brugte "NUMBERS_OF_VEHICELS". Brug "NUMBERS_OF_VEHICLES".
                daily_util_agg["avg_hours_total"] = daily_util_agg["hours_total"] / NUMBERS_OF_VEHICELS
                daily_util_agg["avg_hours_08_17"] = daily_util_agg["hours_08_17"] / NUMBERS_OF_VEHICELS

                # 8) Til reference: antal biler
                daily_util_agg["Antal biler"] = NUMBERS_OF_VEHICELS

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

                util_per_lokation["udnyttelse_pct_08_17"] = (util_per_lokation["total_hours_08_17"] / (num_workdays * WORKDAY_HOURS)).clip(upper=1.0) * 100.0

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
                st.caption("Viser gennemsnitligt og minimalt antal ledige biler i halv-times intervaller mellem 07:00 og 17:30 for det valgte datointerval og filtre.")

                # Arbejds-kopi, datotyper og datointerval
                util_trips = filtered_util.copy()
                util_trips[COLS["start"]] = pd.to_datetime(util_trips[COLS["start"]], errors="coerce")
                util_trips[COLS["end"]] = pd.to_datetime(util_trips[COLS["end"]], errors="coerce")
                util_trips = util_trips.dropna(subset=[COLS["start"], COLS["end"], COLS["reg"]]).copy()

                # Rensning: start <= slut
                mask_swap = util_trips[COLS["end"]] < util_trips[COLS["start"]]
                if mask_swap.any():
                    tmp = util_trips.loc[mask_swap, COLS["start"]].copy()
                    util_trips.loc[mask_swap, COLS["start"]] = util_trips.loc[mask_swap, COLS["end"]]
                    util_trips.loc[mask_swap, COLS["end"]] = tmp

                # Dato-interval fra UI
                dmin = pd.to_datetime(selected_min)
                dmax = pd.to_datetime(selected_max)
                if dmin > dmax:
                    dmin, dmax = dmax, dmin

                # Filtrer ture til overlappende med dato-interval (for performance)
                util_trips = util_trips[(util_trips[COLS["end"]] >= dmin) & (util_trips[COLS["start"]] <= dmax + pd.Timedelta(days=1))]

                # Biler i udvalget (flådestørrelse)
                fleet_size = int(util_trips[COLS["reg"]].nunique())

                # Tids-slots hver 30. minut fra 07:00 til og med 17:30
                slot_start = datetime.combine(datetime.today().date(), time(8, 0))
                slot_end = datetime.combine(datetime.today().date(), time(17, 0))
                half_hour = timedelta(minutes=30)
                slots = []
                t = slot_start
                while t <= slot_end:
                    slots.append(t.time())
                    t += half_hour

                # Alle kalenderdage i intervallet
                all_days = pd.date_range(dmin.normalize(), dmax.normalize(), freq="D")

                # Optælling af ledige pr. dag og slot
                rec = []
                for day in all_days:
                    for tt in slots:
                        ws = datetime.combine(day.date(), tt)
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
                
                util_per_vehicle["udnyttelse_pct_08_17"] = (util_per_vehicle["total_hours_08_17"] / (num_workdays * WORKDAY_HOURS)).clip(upper=1.0) * 100.0
                util_per_vehicle["udnyttelse_pct_16_timer"] = (util_per_vehicle["total_hours"] / (num_workdays * 16)).clip(upper=1.0) * 100.0
                util_per_vehicle["udnyttelse_pct_24_timer"] = (util_per_vehicle["total_hours"] / (num_workdays * 24)).clip(upper=1.0) * 100.0

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

        with tabs[5]:
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
