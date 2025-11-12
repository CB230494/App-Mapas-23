# -*- coding: utf-8 -*-
# ================================================================
# CASOS DE ÉXITO CR – Streamlit + Google Sheets + Folium + Altair
# ================================================================

import os, io, json, uuid, re
from datetime import datetime, date
import numpy as np
import pandas as pd
import streamlit as st

import gspread
from gspread.exceptions import APIError
from google.oauth2.service_account import Credentials

import requests
import folium
from folium.plugins import HeatMap, MarkerCluster
from streamlit_folium import st_folium
import altair as alt

# ---------- Config básica ----------
st.set_page_config(page_title="Casos de Éxito – Costa Rica", page_icon="🗺️", layout="wide")
RERUN = getattr(st, "rerun", None) or getattr(st, "experimental_rerun", None)

# ---------- Parámetros generales ----------
SHEET_ID = os.getenv("SHEET_ID", "1jLq0TeCc6x2OXnWC2I_A4f1kwg5Zgfd665v5Bm9IYSw")
WS_NAME  = os.getenv("WS_NAME", "casos_exito")

HEADERS = [
    "id","timestamp","titulo","descripcion","categoria","impacto",
    "responsable","institucion","fecha_evento",
    "provincia","canton","distrito","lat","lon",
    "etiquetas","evidencia_url","estado"
]

DEFAULT_CATEGORIAS = ["Seguridad","Comunidad","Prevención","Operativo","Gestión"]
DEFAULT_IMPACTO    = ["Alto","Medio","Bajo"]
DEFAULT_ESTADO     = ["Activo","Archivado"]
CR_CENTER = (9.748917, -83.753428)  # centro aprox CR

# ---------- Mapas base (con attribution correcto) ----------
BASEMAPS = {
    "OpenStreetMap": folium.TileLayer(tiles="OpenStreetMap", name="OpenStreetMap", control=True),
    "CartoDB Positron": folium.TileLayer(
        tiles="CartoDB positron", name="CartoDB Positron", control=True,
        attr="© OpenStreetMap contributors, © CARTO"
    ),
    "CartoDB Dark Matter": folium.TileLayer(
        tiles="CartoDB dark_matter", name="CartoDB Dark Matter", control=True,
        attr="© OpenStreetMap contributors, © CARTO"
    ),
    "Esri Street": folium.TileLayer(
        tiles="https://server.arcgisonline.com/ArcGIS/rest/services/World_Street_Map/MapServer/tile/{z}/{y}/{x}",
        name="Esri Street", control=True, attr="Sources: Esri, USGS, NOAA, etc."
    ),
    "Esri Topo": folium.TileLayer(
        tiles="https://server.arcgisonline.com/ArcGIS/rest/services/World_Topo_Map/MapServer/tile/{z}/{y}/{x}",
        name="Esri Topo", control=True, attr="Sources: Esri, USGS, NOAA, etc."
    ),
    "Esri Satélite": folium.TileLayer(
        tiles="https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
        name="Esri Satélite", control=True, attr="Sources: Esri, i-cubed, USDA, USGS, AEX, GeoEye, etc."
    ),
    "OSM France": folium.TileLayer(
        tiles="https://{s}.tile.openstreetmap.fr/osmfr/{z}/{x}/{y}.png",
        name="OSM France", control=True, attr="© OSM France, © OSM contributors"
    ),
    "Stamen Toner Lite": folium.TileLayer(
        tiles="https://stamen-tiles.a.ssl.fastly.net/toner-lite/{z}/{x}/{y}.png",
        name="Stamen Toner Lite", control=True,
        attr="Map tiles by Stamen Design (CC BY 3.0). Data © OSM contributors"
    ),
    "Stamen Terrain": folium.TileLayer(
        tiles="Stamen Terrain", name="Stamen Terrain", control=True,
        attr="Map tiles by Stamen Design (CC BY 3.0). Data © OSM contributors"
    ),
    "Stamen Watercolor": folium.TileLayer(
        tiles="Stamen Watercolor", name="Stamen Watercolor", control=True,
        attr="Map tiles by Stamen Design (CC BY 3.0). Data © OSM contributors"
    ),
    "OpenTopoMap": folium.TileLayer(
        tiles="https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png",
        name="OpenTopoMap", control=True,
        attr="© OpenTopoMap (CC-BY-SA), © OSM contributors"
    ),
    "HikeBike": folium.TileLayer(
        tiles="https://tiles.wmflabs.org/hikebike/{z}/{x}/{y}.png",
        name="HikeBike", control=True,
        attr="© Hike & Bike Map, © OSM contributors"
    ),
}

# ---------- Conexión robusta a Google Sheets ----------
@st.cache_resource(show_spinner=False)
def _get_gs_client_or_none():
    try:
        creds = Credentials.from_service_account_info(
            dict(st.secrets["gcp_service_account"]),
            scopes=["https://www.googleapis.com/auth/spreadsheets",
                    "https://www.googleapis.com/auth/drive"]
        )
        return gspread.authorize(creds)
    except Exception:
        st.warning("No se pudo autorizar Google Sheets. Modo sin escritura.")
        return None

def _open_or_create_worksheet(gc):
    if gc is None: return None
    try:
        sh = gc.open_by_key(SHEET_ID)
        try:
            ws = sh.worksheet(WS_NAME)
        except gspread.WorksheetNotFound:
            ws = sh.add_worksheet(title=WS_NAME, rows=1000, cols=len(HEADERS))
            ws.append_row(HEADERS)
        hdr = [h.strip().lower() for h in ws.row_values(1)]
        if hdr != [h.lower() for h in HEADERS]:
            ws.resize(rows=max(2, ws.row_count), cols=len(HEADERS))
            ws.update("A1:Q1", [HEADERS])  # 17 columnas -> Q
        return ws
    except APIError:
        st.warning("No se puede acceder a la Hoja (permiso/ID). Modo lectura/local.")
        return None
    except Exception:
        st.warning("Error al abrir la Hoja. Modo lectura/local.")
        return None

def _read_df(ws) -> pd.DataFrame:
    if ws is None: return pd.DataFrame(columns=HEADERS)
    try:
        data = ws.get_all_records()
    except Exception:
        data = []
    df = pd.DataFrame(data) if data else pd.DataFrame(columns=HEADERS)
    for col in ("lat","lon"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    if "fecha_evento" in df.columns:
        df["fecha_evento"] = pd.to_datetime(df["fecha_evento"], errors="coerce").dt.date
    if "timestamp" in df.columns:
        df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
    for h in HEADERS:
        if h not in df.columns: df[h] = np.nan
    return df[HEADERS].copy()

def _append_row(ws, record: dict):
    if ws is None: raise RuntimeError("Sin conexión de escritura")
    ws.append_row([record.get(h, "") for h in HEADERS])

def _find_row_index_by_id(ws, _id: str):
    if ws is None: return None
    for i, val in enumerate(ws.col_values(1), start=1):
        if val == _id: return i
    return None

def _update_row_by_id(ws, _id: str, new_record: dict) -> bool:
    if ws is None: return False
    idx = _find_row_index_by_id(ws, _id)
    if not idx: return False
    ws.update(f"A{idx}:Q{idx}", [[new_record.get(h, "") for h in HEADERS]])
    return True

def _delete_row_by_id(ws, _id: str) -> bool:
    if ws is None: return False
    idx = _find_row_index_by_id(ws, _id)
    if not idx: return False
    ws.delete_rows(idx)
    return True

# --- Alias de compatibilidad por si alguna parte usa el nombre viejo ---
_get_gs_client = _get_gs_client_or_none
# ================================================================
# Parte 2: UI lateral, helpers, conexión y filtros robustos
# ================================================================

def options_or_default(df: pd.DataFrame, col: str, default: list[str]) -> list[str]:
    if col not in df.columns: return list(default)
    vals = df[col].dropna().unique().tolist()
    vals = [v for v in vals if str(v).strip()]
    return sorted(vals) or list(default)

# Helpers de color (si no existen)
if "_color_for_category" not in globals():
    def _color_for_category(cat: str, palette: dict) -> str:
        if isinstance(palette, dict) and cat in palette and palette[cat]:
            return palette[cat]
        h = abs(hash(str(cat))) % 360
        return f"hsl({h},70%,45%)"

# ----- Sidebar: configuración y paleta -----
with st.sidebar:
    st.header("⚙️ Configuración")
    st.caption("Fuente: Google Sheets")
    st.write(f"Worksheet: `{WS_NAME}`")

# Conexión y carga
gc = _get_gs_client_or_none()
ws = _open_or_create_worksheet(gc)
df = _read_df(ws)

# Diagnóstico visible
with st.sidebar:
    try:
        _ = ws.title if ws is not None else None
        state_conn = "🟢 Conexión OK (lectura/escritura)" if ws is not None else "🔴 Sin conexión"
    except Exception:
        state_conn = "🟡 Lectura posible, escritura dudosa"
    st.caption(f"Estado conexión: {state_conn}")

    st.subheader("🎨 Paleta por categoría")
    if "palette" not in st.session_state:
        st.session_state.palette = {c: "#1f77b4" for c in DEFAULT_CATEGORIAS}
    for c in DEFAULT_CATEGORIAS:
        st.session_state.palette[c] = st.color_picker(
            c, st.session_state.palette.get(c, "#1f77b4"), key=f"palette_{c}"
        )

# ----- Filtros -----
with st.sidebar:
    st.subheader("🔎 Filtros")
    min_date = date(2020, 1, 1)
    max_date = date.today()
    if "fecha_evento" in df.columns and not df["fecha_evento"].dropna().empty:
        min_date = min(min_date, df["fecha_evento"].dropna().min())
        max_date = max(max_date, df["fecha_evento"].dropna().max())

    rango_fecha = st.date_input("Rango de fechas", (min_date, max_date))
    f_prov   = st.multiselect("Provincia", options_or_default(df, "provincia", []))
    f_canton = st.multiselect("Cantón",    options_or_default(df, "canton", []))
    f_cat    = st.multiselect("Categoría", options_or_default(df, "categoria", DEFAULT_CATEGORIAS))
    f_imp    = st.multiselect("Impacto",   options_or_default(df, "impacto",   DEFAULT_IMPACTO))
    f_estado = st.multiselect("Estado",    options_or_default(df, "estado",    DEFAULT_ESTADO))
    texto    = st.text_input("Buscar (título/descr./etiquetas)")

def _apply_filters(df0: pd.DataFrame) -> pd.DataFrame:
    dff = df0.copy()
    if isinstance(rango_fecha, tuple) and len(rango_fecha) == 2:
        ini, fin = rango_fecha
        dff = dff[(dff["fecha_evento"].isna()) | ((dff["fecha_evento"] >= ini) & (dff["fecha_evento"] <= fin))]
    if f_prov:   dff = dff[dff["provincia"].isin(f_prov)]
    if f_canton: dff = dff[dff["canton"].isin(f_canton)]
    if f_cat:    dff = dff[dff["categoria"].isin(f_cat)]
    if f_imp:    dff = dff[dff["impacto"].isin(f_imp)]
    if f_estado: dff = dff[dff["estado"].isin(f_estado)]
    if texto:
        patt = re.compile(re.escape(texto), re.IGNORECASE)
        cols = [c for c in ["titulo","descripcion","etiquetas"] if c in dff.columns]
        if cols:
            dff = dff[dff[cols].astype(str).apply(lambda r: any(patt.search(x) for x in r), axis=1)]
    return dff

# ----- Pestañas -----
tab_reg, tab_map, tab_charts, tab_export = st.tabs(
    ["📝 Registrar / Admin", "🗺️ Mapa", "📈 Gráficas", "⬇️ Exportar"]
)
# ================================================================
# Parte 3: Gráficas (KPIs, serie por mes, categorías e impacto)
# ================================================================

# Helper de mes seguro
if "_month_floor" not in globals():
    from datetime import datetime as _dt, date as _date
    def _month_floor(x):
        if x is None or (isinstance(x, float) and np.isnan(x)): return pd.NaT
        if isinstance(x, (_dt, _date)): return _date(x.year, x.month, 1)
        dt = pd.to_datetime(x, errors="coerce")
        if pd.isna(dt): return pd.NaT
        return _date(dt.year, dt.month, 1)

with tab_charts:
    st.subheader("Gráficas")
    dff = _apply_filters(df)

    total_casos = int(len(dff))
    total_act = int((dff["estado"]=="Activo").sum()) if "estado" in dff.columns else 0
    total_arc = int((dff["estado"]=="Archivado").sum()) if "estado" in dff.columns else 0
    c1,c2,c3 = st.columns(3)
    c1.metric("Total de casos", f"{total_casos}")
    c2.metric("Activos", f"{total_act}")
    c3.metric("Archivados", f"{total_arc}")

    st.markdown("### Casos por mes")
    if not dff.empty and "fecha_evento" in dff.columns:
        ts = dff.copy()
        ts["mes"] = ts["fecha_evento"].apply(_month_floor)
        ts = ts.dropna(subset=["mes"])
        if not ts.empty:
            serie = ts.groupby("mes")["id"].count().reset_index().rename(columns={"id":"casos"})
            chart = (alt.Chart(serie).mark_line(point=True)
                     .encode(x=alt.X("mes:T", title="Mes"),
                             y=alt.Y("casos:Q", title="Casos"),
                             tooltip=["mes:T","casos:Q"]).properties(height=300))
            st.altair_chart(chart, use_container_width=True)
        else:
            st.info("No hay fechas válidas para graficar por mes.")
    else:
        st.info("No hay datos de `fecha_evento` para graficar.")

    st.markdown("### Casos por categoría")
    if not dff.empty and "categoria" in dff.columns:
        cat = (dff.copy()
               .assign(categoria=lambda x: x["categoria"].fillna("Sin categoría"))
               .groupby("categoria")["id"].count().reset_index()
               .rename(columns={"id":"casos"}).sort_values("casos", ascending=False))
        if not cat.empty:
            bars = (alt.Chart(cat).mark_bar()
                    .encode(x=alt.X("casos:Q", title="Casos"),
                            y=alt.Y("categoria:N", sort="-x", title="Categoría"),
                            tooltip=["categoria:N","casos:Q"])
                    .properties(height=max(220, 24*len(cat))))
            st.altair_chart(bars, use_container_width=True)
        else:
            st.info("No hay categorías para mostrar.")
    else:
        st.info("No hay datos de `categoria`.")

    st.markdown("### Distribución por impacto")
    if not dff.empty and "impacto" in dff.columns:
        imp = (dff.copy().assign(impacto=lambda x: x["impacto"].fillna("Sin dato"))
               .groupby("impacto")["id"].count().reset_index()
               .rename(columns={"id":"casos"}))
        if not imp.empty:
            pie = (alt.Chart(imp).mark_arc()
                   .encode(theta="casos:Q", color=alt.Color("impacto:N", legend=None),
                           tooltip=["impacto:N","casos:Q"]).properties(height=300))
            legend = (alt.Chart(imp).mark_rect()
                      .encode(y=alt.Y("impacto:N", title="Impacto"),
                              color=alt.Color("impacto:N", legend=None)))
            st.altair_chart(pie | legend, use_container_width=True)
        else:
            st.info("Sin datos de impacto.")
    else:
        st.info("No hay datos de `impacto`.")
# ================================================================
# Parte 4: Mapa Folium – cluster, heatmap y coropleta (GeoJSON)
# ================================================================

with tab_map:
    st.subheader("Mapa de Casos de Éxito – Costa Rica")

    left, right = st.columns([1,2])
    with left:
        zoom = st.slider("Zoom inicial", 5, 12, 7)
        base_keys = list(BASEMAPS.keys())
        default_base_idx = base_keys.index("CartoDB Positron") if "CartoDB Positron" in base_keys else 0
        base_choice = st.selectbox("Mapa base", base_keys, index=default_base_idx)
        use_cluster = st.checkbox("Agrupar marcadores (Cluster)", value=True)
        show_heat = st.checkbox("Capa Heatmap", value=True)

        st.markdown("**Capa de áreas (GeoJSON provincias/cantones – opcional)**")
        default_geojson = "https://rawcdn.githack.com/juanmamoralesp/cr-geojson/refs/heads/main/provincias.geojson"
        geojson_url = st.text_input("URL GeoJSON (opcional)", value=default_geojson)
        choropleth_on = st.checkbox("Mostrar coropleta por conteo/impacto", value=True)
        color_metric = st.selectbox("Métrica de color", ["conteo (por área)", "impacto promedio"], index=0)
        geojson_file = st.file_uploader("o sube un .geojson / .json", type=["geojson", "json"])
        st.caption("Si el GeoJSON no carga, el mapa igual mostrará puntos y Heatmap.")

    dff = _apply_filters(df)

    with right:
        # --- Mapa base con fallbacks ---
        m = folium.Map(location=CR_CENTER, zoom_start=zoom, control_scale=True, tiles=None)

        def _safe_add_base(layer_name: str):
            try:
                BASEMAPS[layer_name].add_to(m); return layer_name
            except Exception: pass
            try:
                folium.TileLayer(
                    tiles="https://tile.openstreetmap.org/{z}/{x}/{y}.png",
                    name="OpenStreetMap (fallback)", attr="© OpenStreetMap contributors"
                ).add_to(m); return "OpenStreetMap (fallback)"
            except Exception: pass
            try:
                folium.TileLayer(
                    tiles="https://server.arcgisonline.com/ArcGIS/rest/services/World_Street_Map/MapServer/tile/{z}/{y}/{x}",
                    name="Esri Street (fallback)", attr="Sources: Esri, USGS, NOAA, etc."
                ).add_to(m); return "Esri Street (fallback)"
            except Exception:
                return None

        used_base = _safe_add_base(base_choice)
        if not used_base: st.error("No fue posible cargar ningún mapa base.")

        # --- Marcadores ---
        points = dff.dropna(subset=["lat","lon"]) if not dff.empty else pd.DataFrame(columns=["lat","lon"])
        _palette = st.session_state.get("palette", {c: "#1f77b4" for c in DEFAULT_CATEGORIAS})
        if use_cluster: cluster = MarkerCluster(name="Casos (cluster)").add_to(m)

        for _, r in points.iterrows():
            color = _color_for_category(r.get("categoria",""), _palette)
            popup_html = (
                f"<b>{r.get('titulo','Caso')}</b><br>{(r.get('descripcion','') or '')[:300]}<br>"
                f"<i>{r.get('categoria','')} • {r.get('impacto','')} • {r.get('fecha_evento','')}</i><br>"
                f"{r.get('provincia','')} / {r.get('canton','')} / {r.get('distrito','')}<br>"
                f"{'📎 <a href=\"'+str(r.get('evidencia_url'))+'\" target=\"_blank\">Evidencia</a>' if r.get('evidencia_url') else ''}"
            )
            marker = folium.CircleMarker(
                location=(float(r["lat"]), float(r["lon"])),
                radius=8, color=color, fill=True, fill_color=color, fill_opacity=0.8,
                tooltip=r.get("titulo","Caso"), popup=folium.Popup(popup_html, max_width=350)
            )
            if use_cluster: marker.add_to(cluster)
            else: marker.add_to(m)

        # --- Heatmap ---
        if show_heat and not points.empty:
            impact_w = {"Alto":1.0, "Medio":0.6, "Bajo":0.3}
            heat_data = [[float(row["lat"]), float(row["lon"]), impact_w.get(str(row.get("impacto")),0.5)]
                         for _, row in points.iterrows()]
            HeatMap(heat_data, name="Heatmap", radius=20, blur=15, max_zoom=12).add_to(m)

        # --- Coropleta (GeoJSON) ---
        gj_obj = None
        if choropleth_on:
            if geojson_file is not None:
                try: gj_obj = json.load(geojson_file)
                except Exception: st.warning("Archivo GeoJSON inválido.")
            elif geojson_url.strip():
                def _fetch(url):
                    resp = requests.get(url, headers={"User-Agent":"Mozilla/5.0"}, timeout=25)
                    resp.raise_for_status(); return resp.json()
                candidates = [geojson_url]
                if "github.com/" in geojson_url:
                    candidates.append(geojson_url.replace("github.com/","raw.githubusercontent.com/").replace("/blob/","/"))
                    if "/blob/" in geojson_url:
                        owner_repo_path = geojson_url.split("github.com/")[1].split("/blob/")[0]
                        branch_path = geojson_url.split("/blob/")[1]
                        candidates.append(f"https://raw.githubusercontent.com/{owner_repo_path}/{branch_path}")
                for u in candidates:
                    try: gj_obj = _fetch(u); break
                    except Exception: continue

        if choropleth_on and gj_obj and "features" in gj_obj and len(gj_obj["features"])>0:
            props = gj_obj["features"][0].get("properties", {})
            candidate_keys = ["name","NOM_PROV","provincia","PROVINCIA","NOM_CANT","canton","CANTON"]
            area_key = next((k for k in candidate_keys if k in props), None)

            if area_key:
                if area_key.lower().startswith(("nom_cant","canton")):
                    area_df = dff.copy(); area_df["area"] = area_df["canton"].fillna("")
                else:
                    area_df = dff.copy(); area_df["area"] = area_df["provincia"].fillna("")

                if color_metric.startswith("impacto"):
                    mp = {"Alto":1.0,"Medio":0.6,"Bajo":0.3}
                    map_weight = area_df.groupby("area")["impacto"].apply(
                        lambda s: float(np.mean([mp.get(str(x),0.5) for x in s])) if len(s) else 0.0)
                else:
                    map_weight = area_df.groupby("area")["id"].count()

                map_df = map_weight.reset_index().rename(columns={0:"valor","id":"valor"})
                map_df["area_norm"] = map_df["area"].str.strip().str.lower()

                folium.GeoJson(
                    gj_obj, name="Áreas (referencia)",
                    style_function=lambda x: {"fillColor":"#eeeeee","color":"#555","weight":1,"fillOpacity":0.6},
                    highlight_function=lambda x: {"weight":2,"color":"#222"},
                    tooltip=folium.GeoJsonTooltip(fields=[area_key], aliases=["Área"])
                ).add_to(m)

                from branca.colormap import linear
                if len(map_df):
                    vmin, vmax = float(map_df["valor"].min()), float(map_df["valor"].max())
                    if vmin == vmax: vmin, vmax = (0.0, vmin or 1.0)
                    cmap = linear.YlOrRd_09.scale(vmin, vmax)

                    def choropleth_style(feature):
                        name_val = str(feature["properties"].get(area_key,"")).strip().lower()
                        row = map_df[map_df["area_norm"] == name_val]
                        if not row.empty:
                            val = float(row["valor"].values[0])
                            return {"fillColor": cmap(val), "color":"#444","weight":1,"fillOpacity":0.7}
                        else:
                            return {"fillColor": "#dddddd","color":"#444","weight":1,"fillOpacity":0.3}

                    folium.GeoJson(gj_obj, name="Coropleta (auto)", style_function=choropleth_style).add_to(m)
                    cmap.caption = "Intensidad por área"; cmap.add_to(m)

        folium.LayerControl(collapsed=False).add_to(m)
        st_folium(m, use_container_width=True, height=650)
# ================================================================
# Parte 5: Registrar / Admin  +  Exportar
# ================================================================

def _blank_record():
    return {
        "id": str(uuid.uuid4()),
        "timestamp": datetime.utcnow().isoformat(timespec="seconds"),
        "titulo": "",
        "descripcion": "",
        "categoria": DEFAULT_CATEGORIAS[0],
        "impacto": DEFAULT_IMPACTO[0],
        "responsable": "",
        "institucion": "",
        "fecha_evento": date.today(),
        "provincia": "",
        "canton": "",
        "distrito": "",
        "lat": np.nan,
        "lon": np.nan,
        "etiquetas": "",
        "evidencia_url": "",
        "estado": DEFAULT_ESTADO[0],
    }

with tab_reg:
    st.subheader("Registrar / Admin")

    if "local_buffer" not in st.session_state:
        st.session_state.local_buffer = pd.DataFrame(columns=HEADERS)

    with st.form("form_alta", clear_on_submit=True):
        st.markdown("### Nuevo caso de éxito")

        c1, c2 = st.columns([2, 1])
        with c1:
            titulo = st.text_input("Título*", max_chars=120)
            descripcion = st.text_area("Descripción", height=120)
            etiquetas = st.text_input("Etiquetas (coma separadas)", placeholder="operativo, comunidad, ...")
            evidencia_url = st.text_input("URL de evidencia (opcional)")
        with c2:
            categoria = st.selectbox("Categoría", DEFAULT_CATEGORIAS)
            impacto = st.selectbox("Impacto", DEFAULT_IMPACTO, index=0)
            estado = st.selectbox("Estado", DEFAULT_ESTADO, index=0)
            fecha_evento = st.date_input("Fecha del evento", value=date.today())

        c3, c4, c5 = st.columns(3)
        with c3: provincia = st.text_input("Provincia")
        with c4: canton    = st.text_input("Cantón")
        with c5: distrito  = st.text_input("Distrito")

        st.markdown("**Georreferenciación**")
        g1, g2 = st.columns(2)
        with g1: lat = st.number_input("Latitud", value=0.0, format="%.8f")
        with g2: lon = st.number_input("Longitud", value=0.0, format="%.8f")

        submitted = st.form_submit_button("➕ Guardar caso")

        if submitted:
            rec = _blank_record()
            rec.update({
                "titulo": titulo.strip(),
                "descripcion": descripcion.strip(),
                "categoria": categoria,
                "impacto": impacto,
                "fecha_evento": fecha_evento,
                "provincia": provincia.strip(),
                "canton": canton.strip(),
                "distrito": distrito.strip(),
                "lat": float(lat) if lat else np.nan,
                "lon": float(lon) if lon else np.nan,
                "etiquetas": etiquetas.strip(),
                "evidencia_url": evidencia_url.strip(),
                "estado": estado,
            })

            if not rec["titulo"]:
                st.error("El título es obligatorio.")
            else:
                try:
                    if ws is not None:
                        _append_row(ws, rec)
                        st.success("✅ Caso guardado en Google Sheets.")
                        # refrescar df
                        df = _read_df(ws)
                    else:
                        raise RuntimeError("Sin conexión de escritura")
                except Exception:
                    st.warning("No hay escritura en la hoja. Se guardó en un buffer local para esta sesión.")
                    st.session_state.local_buffer = pd.concat(
                        [st.session_state.local_buffer, pd.DataFrame([rec])], ignore_index=True
                    )
                if RERUN: RERUN()

    df_comb = df.copy()
    if not st.session_state.local_buffer.empty:
        df_comb = pd.concat([df_comb, st.session_state.local_buffer[HEADERS]], ignore_index=True)

    st.markdown("### Registros (vista rápida)")
    if df_comb.empty:
        st.info("No hay registros. Crea el primero con el formulario.")
    else:
        show_cols = ["titulo","categoria","impacto","provincia","canton","distrito",
                     "fecha_evento","lat","lon","estado","evidencia_url"]
        show_cols = [c for c in show_cols if c in df_comb.columns]
        st.dataframe(df_comb[show_cols].sort_values("fecha_evento", ascending=False),
                     use_container_width=True, height=360)

    if st.button("🔄 Recargar datos"):
        if ws is not None: df = _read_df(ws)
        if RERUN: RERUN()

with tab_export:
    st.subheader("Exportar")
    dff = _apply_filters(df)
    df_comb = dff.copy()
    if not st.session_state.local_buffer.empty:
        buf = st.session_state.local_buffer.copy()
        try: buf["fecha_evento"] = pd.to_datetime(buf["fecha_evento"], errors="coerce").dt.date
        except Exception: pass
        if isinstance(rango_fecha, tuple) and len(rango_fecha) == 2:
            ini, fin = rango_fecha
            buf = buf[(buf["fecha_evento"].isna()) | ((buf["fecha_evento"]>=ini) & (buf["fecha_evento"]<=fin))]
        df_comb = pd.concat([df_comb, buf[HEADERS]], ignore_index=True)

    st.write(f"Registros a exportar: **{len(df_comb)}**")
    col1, col2 = st.columns(2)
    with col1:
        csv_bytes = df_comb.to_csv(index=False).encode("utf-8")
        st.download_button("⬇️ CSV", data=csv_bytes, file_name="casos_exito.csv", mime="text/csv")
    with col2:
        xlsx = io.BytesIO()
        with pd.ExcelWriter(xlsx, engine="xlsxwriter") as writer:
            df_comb.to_excel(writer, index=False, sheet_name="casos")
        st.download_button("⬇️ Excel", data=xlsx.getvalue(), file_name="casos_exito.xlsx",
                           mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")




