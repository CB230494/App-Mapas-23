# -*- coding: utf-8 -*-
# ================================================================
# CASOS DE ÉXITO CR – Streamlit + Google Sheets + Folium + Altair
# ------------------------------------------------
# Parte 1: Configuración, imports, constantes y conexión robusta
# ================================================================

import uuid, json, io, re, os
from datetime import datetime, date
import numpy as np
import pandas as pd
import streamlit as st

import gspread
from google.oauth2.service_account import Credentials
from gspread.exceptions import APIError

import folium
from folium.plugins import HeatMap, MarkerCluster
from streamlit_folium import st_folium
import altair as alt
import requests  # carga GeoJSON por URL (con User-Agent + reintentos)

# ---------- Config básica ----------
st.set_page_config(page_title="Casos de Éxito – Costa Rica",
                   page_icon="🗺️", layout="wide")

# Compat con versiones de Streamlit (st.rerun vs experimental_rerun)
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
CR_CENTER = (9.748917, -83.753428)  # Centro aproximado de Costa Rica

# ---------- Mapas base (12 opciones) con attribution correcto ----------
BASEMAPS = {
    # OpenStreetMap (los más confiables en Streamlit Cloud)
    "OpenStreetMap": folium.TileLayer(tiles="OpenStreetMap", name="OpenStreetMap", control=True),
    "CartoDB Positron": folium.TileLayer(
        tiles="CartoDB positron", name="CartoDB Positron", control=True,
        attr="© OpenStreetMap contributors, © CARTO"
    ),
    "CartoDB Dark Matter": folium.TileLayer(
        tiles="CartoDB dark_matter", name="CartoDB Dark Matter", control=True,
        attr="© OpenStreetMap contributors, © CARTO"
    ),
    # Esri (muy estables)
    "Esri Street": folium.TileLayer(
        tiles="https://server.arcgisonline.com/ArcGIS/rest/services/World_Street_Map/MapServer/tile/{z}/{y}/{x}",
        name="Esri Street", control=True,
        attr="Sources: Esri, USGS, NOAA, etc."
    ),
    "Esri Topo": folium.TileLayer(
        tiles="https://server.arcgisonline.com/ArcGIS/rest/services/World_Topo_Map/MapServer/tile/{z}/{y}/{x}",
        name="Esri Topo", control=True,
        attr="Sources: Esri, USGS, NOAA, etc."
    ),
    "Esri Satélite": folium.TileLayer(
        tiles="https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
        name="Esri Satélite", control=True,
        attr="Sources: Esri, i-cubed, USDA, USGS, AEX, GeoEye, etc."
    ),
    # Stamen / otros (algunos CDN pueden fallar en cloud; por eso no los dejo por defecto)
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

# ---------- Conexión a Google Sheets (robusta) ----------
@st.cache_resource(show_spinner=False)
def _get_gs_client_or_none():
    """Autoriza gspread usando SOLO [gcp_service_account].
    Devuelve None si falta secrets o hay fallo de auth (no detiene la app)."""
    try:
        creds = Credentials.from_service_account_info(
            dict(st.secrets["gcp_service_account"]),
            scopes=["https://www.googleapis.com/auth/spreadsheets",
                    "https://www.googleapis.com/auth/drive"]
        )
        return gspread.authorize(creds)
    except Exception as e:
        st.warning("No se pudo autorizar Google Sheets. La app seguirá en modo solo visualización.")
        return None

def _open_or_create_worksheet(gc):
    """Intenta abrir la worksheet. Si falla, devuelve None y la app sigue con DataFrame vacío."""
    if gc is None:
        return None
    try:
        sh = gc.open_by_key(SHEET_ID)
        try:
            ws = sh.worksheet(WS_NAME)
        except gspread.WorksheetNotFound:
            ws = sh.add_worksheet(title=WS_NAME, rows=1000, cols=len(HEADERS))
            ws.append_row(HEADERS)
        # Asegurar encabezados correctos
        hdr = [h.strip().lower() for h in ws.row_values(1)]
        if hdr != [h.lower() for h in HEADERS]:
            ws.resize(rows=max(2, ws.row_count), cols=len(HEADERS))
            ws.update("A1:Q1", [HEADERS])  # 17 columnas -> Q
        return ws
    except APIError:
        st.warning("No se puede acceder a la Hoja (permiso/ID). La app seguirá sin escritura.")
        return None
    except Exception:
        st.warning("Error general al abrir la Hoja. La app seguirá sin escritura.")
        return None

def _read_df(ws) -> pd.DataFrame:
    """Lee registros en DataFrame. Si ws es None, devuelve DataFrame vacío con columnas correctas."""
    if ws is None:
        return pd.DataFrame(columns=HEADERS)
    try:
        data = ws.get_all_records()
    except Exception:
        st.warning("No se pudo leer la Hoja. Mostraré datos vacíos.")
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
        if h not in df.columns:
            df[h] = np.nan
    return df[HEADERS].copy()

def _append_row(ws, record: dict):
    if ws is None:
        st.error("No hay conexión de escritura a la Hoja.")
        return
    ws.append_row([record.get(h, "") for h in HEADERS])

def _find_row_index_by_id(ws, _id: str):
    if ws is None: return None
    for i, val in enumerate(ws.col_values(1), start=1):  # col A
        if val == _id:
            return i
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


# ================================================================
# Parte 2: Utilidades de UI, paletas, filtros, carga inicial y tabs
# ================================================================

def _color_for_category(cat: str, palette: dict) -> str:
    """Color estable por categoría con override desde paleta del sidebar."""
    if cat in palette and palette[cat]:
        return palette[cat]
    h = abs(hash(str(cat))) % 360
    return f"hsl({h},70%,45%)"

def _weight_from_impacto(imp: str) -> float:
    return {"Alto": 1.0, "Medio": 0.6, "Bajo": 0.3}.get(str(imp), 0.5)

def _month_floor(d: date):
    if pd.isna(d): return None
    return date(d.year, d.month, 1)

# ----- Sidebar: configuración y paleta -----
with st.sidebar:
    st.header("⚙️ Configuración")
    st.caption("Fuente: Google Sheets")
    st.write(f"Worksheet: `{WS_NAME}`")
    st.subheader("🎨 Paleta por categoría")
    if "palette" not in st.session_state:
        st.session_state.palette = {c: "#1f77b4" for c in DEFAULT_CATEGORIAS}
    for c in DEFAULT_CATEGORIAS:
        st.session_state.palette[c] = st.color_picker(
            c, st.session_state.palette.get(c, "#1f77b4"), key=f"palette_{c}"
        )

# ----- Conexión y carga de datos (una sola vez) -----
gc = _get_gs_client_or_none()          # <- nombre nuevo
ws = _open_or_create_worksheet(gc)
df = _read_df(ws)

# ----- Filtros globales -----
st.sidebar.subheader("🔎 Filtros")
min_date = date(2020, 1, 1)
max_date = date.today()
if not df["fecha_evento"].dropna().empty:
    min_date = min(min_date, df["fecha_evento"].dropna().min())
    max_date = max(max_date, df["fecha_evento"].dropna().max())

rango_fecha = st.sidebar.date_input("Rango de fechas", (min_date, max_date))
f_prov   = st.sidebar.multiselect("Provincia", sorted(df["provincia"].dropna().unique()))
f_canton = st.sidebar.multiselect("Cantón", sorted(df["canton"].dropna().unique()))
f_cat    = st.sidebar.multiselect("Categoría", sorted(df["categoria"].dropna().unique() or DEFAULT_CATEGORIAS))
f_imp    = st.sidebar.multiselect("Impacto", DEFAULT_IMPACTO)
f_estado = st.sidebar.multiselect("Estado",  DEFAULT_ESTADO or ["Activo"])
texto    = st.sidebar.text_input("Buscar (título/descr./etiquetas)")

def _apply_filters(df0: pd.DataFrame) -> pd.DataFrame:
    dff = df0.copy()
    if isinstance(rango_fecha, tuple) and len(rango_fecha)==2:
        ini, fin = rango_fecha
        dff = dff[(dff["fecha_evento"].isna()) | ((dff["fecha_evento"]>=ini) & (dff["fecha_evento"]<=fin))]
    if f_prov:   dff = dff[dff["provincia"].isin(f_prov)]
    if f_canton: dff = dff[dff["canton"].isin(f_canton)]
    if f_cat:    dff = dff[dff["categoria"].isin(f_cat)]
    if f_imp:    dff = dff[dff["impacto"].isin(f_imp)]
    if f_estado: dff = dff[dff["estado"].isin(f_estado)]
    if texto:
        patt = re.compile(re.escape(texto), re.IGNORECASE)
        cols = ["titulo","descripcion","etiquetas"]
        dff = dff[dff[cols].astype(str).apply(lambda r: any(patt.search(x) for x in r), axis=1)]
    return dff

# ----- Pestañas -----
tab_reg, tab_map, tab_charts, tab_export = st.tabs(
    ["📝 Registrar / Admin", "🗺️ Mapa", "📈 Gráficas", "⬇️ Exportar"]
)

# ================================================================
# Parte 3: Registrar / Administrar registros (CRUD)
# ================================================================

with tab_reg:
    st.subheader("Registrar nuevo caso de éxito")

    cols = st.columns([1,1,1,1])
    with cols[0]:
        titulo = st.text_input("Título *")
        categoria = st.selectbox("Categoría *", sorted(set(DEFAULT_CATEGORIAS + list(df["categoria"].dropna().unique()))))
        impacto = st.selectbox("Impacto *", DEFAULT_IMPACTO, index=1)
        fecha_evento = st.date_input("Fecha del evento *", value=date.today())
    with cols[1]:
        responsable = st.text_input("Responsable")
        institucion = st.text_input("Institución")
        provincia = st.text_input("Provincia")
        canton = st.text_input("Cantón")
    with cols[2]:
        distrito = st.text_input("Distrito")
        etiquetas = st.text_input("Etiquetas (coma/;)")
        evidencia_url = st.text_input("URL de evidencia (foto/video/doc)")
        estado = st.selectbox("Estado", DEFAULT_ESTADO, index=0)
    with cols[3]:
        st.markdown("**Ubicación (click en el mapa o escriba coord.)**")
        lat = st.number_input("Latitud", value=float(CR_CENTER[0]), format="%.6f")
        lon = st.number_input("Longitud", value=float(CR_CENTER[1]), format="%.6f")

    # Mini mapa para capturar coordenadas
    st.markdown("Haga click en el mapa para tomar coordenadas:")
    m_form = folium.Map(location=CR_CENTER, zoom_start=7)
    folium.Marker(CR_CENTER, tooltip="Centro CR").add_to(m_form)
    map_click = st_folium(m_form, height=300, width=None, returned_objects=["last_clicked"])
    if map_click and map_click.get("last_clicked"):
        lat = float(map_click["last_clicked"]["lat"])
        lon = float(map_click["last_clicked"]["lng"])
        st.info(f"Coordenadas seleccionadas: {lat:.6f}, {lon:.6f}")

    desc = st.text_area("Descripción *", height=120)

    btn_cols = st.columns([1,1,3])
    with btn_cols[0]:
        if st.button("➕ Guardar caso", use_container_width=True, type="primary"):
            if not titulo.strip() or not desc.strip():
                st.error("Título y descripción son obligatorios.")
            else:
                rec = {
                    "id": str(uuid.uuid4()),
                    "timestamp": datetime.utcnow().isoformat(),
                    "titulo": titulo.strip(),
                    "descripcion": desc.strip(),
                    "categoria": categoria.strip(),
                    "impacto": impacto.strip(),
                    "responsable": responsable.strip(),
                    "institucion": institucion.strip(),
                    "fecha_evento": fecha_evento.isoformat(),
                    "provincia": provincia.strip(),
                    "canton": canton.strip(),
                    "distrito": distrito.strip(),
                    "lat": lat, "lon": lon,
                    "etiquetas": etiquetas.strip(),
                    "evidencia_url": evidencia_url.strip(),
                    "estado": estado.strip()
                }
                _append_row(ws, rec)
                st.success("Caso guardado correctamente ✅")
                if RERUN: RERUN()

    st.divider()
    st.subheader("Administrar registros")

    dff = _apply_filters(df)
    if dff.empty:
        st.info("No hay registros que cumplan los filtros actuales.")
    else:
        st.dataframe(dff, height=280, use_container_width=True)

        st.markdown("**Editar / Eliminar**")
        selected_id = st.selectbox("Seleccione el ID a editar", [""] + dff["id"].tolist())
        if selected_id:
            rec = dff[dff["id"]==selected_id].iloc[0].to_dict()
            et1, et2 = st.columns([1,1])
            with et1:
                cats = sorted(set(DEFAULT_CATEGORIAS + list(df["categoria"].dropna().unique())))
                new_titulo = st.text_input("Título", rec.get("titulo",""))
                new_categoria = st.selectbox("Categoría", cats, index=(cats.index(rec.get("categoria")) if rec.get("categoria") in cats else 0))
                new_impacto = st.selectbox("Impacto", DEFAULT_IMPACTO, index=(DEFAULT_IMPACTO.index(rec.get("impacto")) if rec.get("impacto") in DEFAULT_IMPACTO else 1))
                new_fecha = st.date_input("Fecha", rec.get("fecha_evento") or date.today())
            with et2:
                new_estado = st.selectbox("Estado", DEFAULT_ESTADO, index=(DEFAULT_ESTADO.index(rec.get("estado")) if rec.get("estado") in DEFAULT_ESTADO else 0))
                new_lat = st.number_input("Lat", value=float(rec["lat"]) if not pd.isna(rec["lat"]) else CR_CENTER[0], format="%.6f")
                new_lon = st.number_input("Lon", value=float(rec["lon"]) if not pd.isna(rec["lon"]) else CR_CENTER[1], format="%.6f")

            new_desc = st.text_area("Descripción", rec.get("descripcion",""), height=120)

            ecols = st.columns(3)
            with ecols[0]:
                if st.button("💾 Guardar cambios", use_container_width=True):
                    rec.update({
                        "titulo": new_titulo,
                        "categoria": new_categoria,
                        "impacto": new_impacto,
                        "fecha_evento": new_fecha.isoformat(),
                        "estado": new_estado,
                        "lat": new_lat, "lon": new_lon,
                        "descripcion": new_desc
                    })
                    if _update_row_by_id(ws, rec["id"], rec):
                        st.success("Actualizado ✅")
                        if RERUN: RERUN()
                    else:
                        st.error("No se encontró el registro para actualizar.")
            with ecols[1]:
                if st.button("🗑️ Eliminar", use_container_width=True):
                    if _delete_row_by_id(ws, rec["id"]):
                        st.warning("Registro eliminado.")
                        if RERUN: RERUN()
                    else:
                        st.error("No se pudo eliminar.")
            with ecols[2]:
                st.caption("Edite lo necesario y luego guarde.")
# ================================================================
# Parte 4: Mapa Folium – cluster, heatmap y coropleta (GeoJSON)
# ================================================================

with tab_map:
    st.subheader("Mapa de Casos de Éxito – Costa Rica")

    left, right = st.columns([1,2])
    with left:
        zoom = st.slider("Zoom inicial", 5, 12, 7)

        # Base por defecto: CartoDB Positron
        base_keys = list(BASEMAPS.keys())
        default_base_idx = base_keys.index("CartoDB Positron") if "CartoDB Positron" in base_keys else 0
        base_choice = st.selectbox("Mapa base", base_keys, index=default_base_idx)

        use_cluster = st.checkbox("Agrupar marcadores (Cluster)", value=True)
        show_heat = st.checkbox("Capa Heatmap", value=True)

        st.markdown("**Capa de áreas (GeoJSON provincias/cantones – opcional)**")
        default_geojson = "https://rawcdn.githack.com/juanmamoralesp/cr-geojson/refs/heads/main/provincias.geojson"
        geojson_url = st.text_input("URL GeoJSON (opcional)", value=default_geojson)

        # Coropleta activada por defecto
        choropleth_on = st.checkbox("Mostrar coropleta por conteo/impacto", value=True)
        color_metric = st.selectbox("Métrica de color", ["conteo (por área)", "impacto promedio"], index=0)

        geojson_file = st.file_uploader("o sube un .geojson / .json", type=["geojson", "json"])
        st.caption("Si el GeoJSON no carga, el mapa igual mostrará puntos y Heatmap.")

    # Datos filtrados (provienen de Parte 2)
    dff = _apply_filters(df)

    with right:
        # --- Mapa base con *fallbacks* para evitar pantalla en blanco ---
        m = folium.Map(location=CR_CENTER, zoom_start=zoom, control_scale=True, tiles=None)

        def _safe_add_base(layer_name: str):
            """Intenta agregar el base layer elegido; si falla, agrega OSM estándar."""
            try:
                BASEMAPS[layer_name].add_to(m)
                return layer_name
            except Exception:
                pass
            # Fallback 1: OSM estándar (URL directa)
            try:
                folium.TileLayer(
                    tiles="https://tile.openstreetmap.org/{z}/{x}/{y}.png",
                    name="OpenStreetMap (fallback)",
                    attr="© OpenStreetMap contributors"
                ).add_to(m)
                return "OpenStreetMap (fallback)"
            except Exception:
                pass
            # Fallback 2: Esri Street
            try:
                folium.TileLayer(
                    tiles="https://server.arcgisonline.com/ArcGIS/rest/services/World_Street_Map/MapServer/tile/{z}/{y}/{x}",
                    name="Esri Street (fallback)",
                    attr="Sources: Esri, USGS, NOAA, etc."
                ).add_to(m)
                return "Esri Street (fallback)"
            except Exception:
                return None

        used_base = _safe_add_base(base_choice)
        if not used_base:
            st.error("No fue posible cargar ningún mapa base. Revisa tu conexión de red.")
        else:
            st.caption(f"Base usada: **{used_base}**")

        # --- Marcadores ---
        points = dff.dropna(subset=["lat", "lon"]) if not dff.empty else pd.DataFrame(columns=["lat", "lon"])
        if use_cluster:
            cluster = MarkerCluster(name="Casos (cluster)").add_to(m)

        for _, r in points.iterrows():
            color = _color_for_category(r.get("categoria", ""), st.session_state.palette)
            popup_html = (
                f"<b>{r.get('titulo','Caso')}</b><br>{(r.get('descripcion','') or '')[:300]}<br>"
                f"<i>{r.get('categoria','')} • {r.get('impacto','')} • {r.get('fecha_evento','')}</i><br>"
                f"{r.get('provincia','')} / {r.get('canton','')} / {r.get('distrito','')}<br>"
                f"{'📎 <a href=\"'+str(r.get('evidencia_url'))+'\" target=\"_blank\">Evidencia</a>' if r.get('evidencia_url') else ''}"
            )
            marker = folium.CircleMarker(
                location=(float(r["lat"]), float(r["lon"])),
                radius=8, color=color, fill=True, fill_color=color, fill_opacity=0.8,
                tooltip=r.get("titulo", "Caso"),
                popup=folium.Popup(popup_html, max_width=350),
            )
            if use_cluster:
                marker.add_to(cluster)
            else:
                marker.add_to(m)

        # --- Heatmap ---
        if show_heat and not points.empty:
            impact_w = {"Alto": 1.0, "Medio": 0.6, "Bajo": 0.3}
            heat_data = [
                [float(row["lat"]), float(row["lon"]), impact_w.get(str(row.get("impacto")), 0.5)]
                for _, row in points.iterrows()
            ]
            HeatMap(heat_data, name="Heatmap", radius=20, blur=15, max_zoom=12).add_to(m)

        # --- Coropleta (GeoJSON) con reintentos y silenciosa si falla ---
        gj_obj = None
        if choropleth_on:
            if geojson_file is not None:
                try:
                    gj_obj = json.load(geojson_file)
                except Exception:
                    st.warning("El archivo GeoJSON subido no es válido.")
            elif geojson_url.strip():
                def _fetch(url):
                    resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=25)
                    resp.raise_for_status()
                    return resp.json()
                candidates = [geojson_url]
                if "github.com/" in geojson_url:
                    candidates.append(
                        geojson_url.replace("github.com/", "raw.githubusercontent.com/").replace("/blob/", "/")
                    )
                    if "/blob/" in geojson_url:
                        owner_repo_path = geojson_url.split("github.com/")[1].split("/blob/")[0]
                        branch_path = geojson_url.split("/blob/")[1]
                        candidates.append(f"https://raw.githubusercontent.com/{owner_repo_path}/{branch_path}")
                for u in candidates:
                    try:
                        gj_obj = _fetch(u)
                        break
                    except Exception:
                        continue
                # Si todo falla, no interrumpimos: solo no pintamos coropleta.

        # --- Pintado por áreas (si hay GeoJSON válido) ---
        if choropleth_on and gj_obj and "features" in gj_obj and len(gj_obj["features"]) > 0:
            props = gj_obj["features"][0].get("properties", {})
            candidate_keys = ["name", "NOM_PROV", "provincia", "PROVINCIA", "NOM_CANT", "canton", "CANTON"]
            area_key = next((k for k in candidate_keys if k in props), None)

            if area_key:
                # Provincia vs Cantón
                if area_key.lower().startswith(("nom_cant", "canton")):
                    area_df = dff.copy(); area_df["area"] = area_df["canton"].fillna("")
                else:
                    area_df = dff.copy(); area_df["area"] = area_df["provincia"].fillna("")

                # Métrica
                if color_metric.startswith("impacto"):
                    mp = {"Alto": 1.0, "Medio": 0.6, "Bajo": 0.3}
                    map_weight = area_df.groupby("area")["impacto"].apply(
                        lambda s: float(np.mean([mp.get(str(x), 0.5) for x in s])) if len(s) else 0.0
                    )
                else:
                    map_weight = area_df.groupby("area")["id"].count()

                map_df = map_weight.reset_index().rename(columns={0: "valor", "id": "valor"})
                map_df["area_norm"] = map_df["area"].str.strip().str.lower()

                # Capa de referencia + tooltip
                folium.GeoJson(
                    gj_obj,
                    name="Áreas (referencia)",
                    style_function=lambda x: {"fillColor": "#eeeeee", "color": "#555", "weight": 1, "fillOpacity": 0.6},
                    highlight_function=lambda x: {"weight": 2, "color": "#222"},
                    tooltip=folium.GeoJsonTooltip(fields=[area_key], aliases=["Área"]),
                ).add_to(m)

                # Pintado por valor (coropleta)
                from branca.colormap import linear
                if len(map_df):
                    vmin, vmax = float(map_df["valor"].min()), float(map_df["valor"].max())
                    if vmin == vmax: vmin, vmax = (0.0, vmin or 1.0)
                    cmap = linear.YlOrRd_09.scale(vmin, vmax)

                    def choropleth_style(feature):
                        name_val = str(feature["properties"].get(area_key, "")).strip().lower()
                        row = map_df[map_df["area_norm"] == name_val]
                        if not row.empty:
                            val = float(row["valor"].values[0])
                            return {"fillColor": cmap(val), "color": "#444", "weight": 1, "fillOpacity": 0.7}
                        else:
                            return {"fillColor": "#dddddd", "color": "#444", "weight": 1, "fillOpacity": 0.3}

                    folium.GeoJson(gj_obj, name="Coropleta (auto)", style_function=choropleth_style).add_to(m)
                    cmap.caption = "Intensidad por área"
                    cmap.add_to(m)

        folium.LayerControl(collapsed=False).add_to(m)
        st_folium(m, use_container_width=True, height=650)


# ================================================================
# Parte 5: Gráficas (Altair) y Exportar (CSV/Excel)
# ================================================================

with tab_charts:
    st.subheader("Análisis y Gráficas")
    dff = _apply_filters(df)
    if dff.empty:
        st.info("No hay datos para graficar con los filtros actuales.")
    else:
        dff["mes"] = dff["fecha_evento"].apply(_month_floor)

        colA, colB, colC = st.columns([1,1,1])
        with colA:
            tipo = st.selectbox("Tipo de gráfico", [
                "Barras por categoría","Barras por provincia","Serie mensual",
                "Top N cantones","Torta por categoría"
            ])
        with colB:
            n_top = st.number_input("Top N (si aplica)", min_value=3, max_value=30, value=10, step=1)
        with colC:
            stacked = st.checkbox("Apilado (si aplica)", value=True)

        cat_list = sorted(dff["categoria"].dropna().unique().tolist() or DEFAULT_CATEGORIAS)
        cat_colors = [st.session_state.palette.get(c, "#1f77b4") for c in cat_list]
        alt.themes.enable("opaque")
        base = alt.Chart(dff)

        if tipo == "Barras por categoría":
            chart = base.mark_bar().encode(
                x=alt.X("count():Q", title="Casos"),
                y=alt.Y("categoria:N", sort="-x", title="Categoría"),
                color=alt.Color("categoria:N", scale=alt.Scale(range=cat_colors), legend=None),
                tooltip=[alt.Tooltip("categoria:N", title="Categoría"), alt.Tooltip("count():Q", title="Casos")]
            ).properties(height=400)

        elif tipo == "Barras por provincia":
            chart = base.mark_bar().encode(
                x=alt.X("count():Q", title="Casos"),
                y=alt.Y("provincia:N", sort="-x", title="Provincia"),
                color=alt.Color("categoria:N", scale=alt.Scale(range=cat_colors)),
                tooltip=["provincia:N","categoria:N","count():Q"]
            ).properties(height=450)

        elif tipo == "Serie mensual":
            g = dff.dropna(subset=["mes"]).groupby(["mes","categoria"])["id"].count().reset_index()
            chart = alt.Chart(g).mark_line(point=True).encode(
                x=alt.X("mes:T", title="Mes"),
                y=alt.Y("id:Q", title="Casos"),
                color=alt.Color("categoria:N", scale=alt.Scale(range=cat_colors)),
                tooltip=["mes:T","categoria:N","id:Q"]
            ).properties(height=400)

        elif tipo == "Top N cantones":
            g = dff.groupby("canton")["id"].count().reset_index().sort_values("id", ascending=False).head(int(n_top))
            chart = alt.Chart(g).mark_bar().encode(
                x=alt.X("id:Q", title="Casos"),
                y=alt.Y("canton:N", sort="-x", title="Cantón"),
                tooltip=["canton:N","id:Q"]
            ).properties(height=450)

        else:  # Torta por categoría
            g = dff.groupby("categoria")["id"].count().reset_index()
            chart = alt.Chart(g).mark_arc(innerRadius=60).encode(
                theta="id:Q",
                color=alt.Color("categoria:N", scale=alt.Scale(range=cat_colors)),
                tooltip=["categoria:N","id:Q"]
            ).properties(height=420)

        st.altair_chart(chart, use_container_width=True)

with tab_export:
    st.subheader("Exportar datos")
    dff = _apply_filters(df)
    st.write(f"Registros filtrados: **{len(dff)}**")

    c1, c2 = st.columns(2)
    with c1:
        csv = dff.to_csv(index=False).encode("utf-8")
        st.download_button("⬇️ Descargar CSV", data=csv, file_name="casos_exito.csv",
                           mime="text/csv", use_container_width=True)
    with c2:
        output = io.BytesIO()
        with pd.ExcelWriter(output, engine="xlsxwriter") as writer:
            dff.to_excel(writer, index=False, sheet_name="casos")
        st.download_button("⬇️ Descargar Excel", data=output.getvalue(),
                           file_name="casos_exito.xlsx",
                           mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                           use_container_width=True)

st.caption("© Casos de Éxito – Costa Rica")





