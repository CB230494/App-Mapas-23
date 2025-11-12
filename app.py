# -*- coding: utf-8 -*-
# ================================================================
# Casos de Éxito – Mapa y Análisis (Streamlit + Google Sheets)
# - CRUD sobre Google Sheets (gspread + service account)
# - Mapa con Folium: basemaps, marcadores (cluster), heatmap, coropleta (GeoJSON)
# - Gráficas Altair con selección de paleta/colores
# - Descarga CSV/Excel
# ================================================================

import uuid, json, io, math, re
from datetime import datetime, date
from dateutil import parser as dtparser

import numpy as np
import pandas as pd
import streamlit as st

import gspread
from google.oauth2.service_account import Credentials

import folium
from folium.plugins import HeatMap, MarkerCluster
from streamlit_folium import st_folium
import altair as alt

# ------------------ Config básica ------------------
st.set_page_config(
    page_title="Casos de Éxito CR",
    page_icon="🗺️",
    layout="wide"
)

SHEET_ID = st.secrets["gsheets"]["sheet_id"]
WS_NAME  = st.secrets["gsheets"].get("worksheet", "casos_exito")

HEADERS = [
    "id","timestamp","titulo","descripcion","categoria","impacto",
    "responsable","institucion","fecha_evento",
    "provincia","canton","distrito",
    "lat","lon","etiquetas","evidencia_url","estado"
]

DEFAULT_CATEGORIAS = ["Seguridad", "Comunidad", "Prevención", "Operativo", "Gestión"]
DEFAULT_IMPACTO    = ["Alto","Medio","Bajo"]
DEFAULT_ESTADO     = ["Activo","Archivado"]

CR_CENTER = (9.748917, -83.753428)  # Centro aproximado Costa Rica

BASEMAPS = {
    "OpenStreetMap": folium.TileLayer(tiles="OpenStreetMap", control=True, name="OSM"),
    "CartoDB Positron": folium.TileLayer(tiles="CartoDB Positron", control=True, name="CartoDB Positron"),
    "CartoDB Dark": folium.TileLayer(tiles="CartoDB Dark_Matter", control=True, name="CartoDB Dark"),
    "Stamen Terrain": folium.TileLayer(tiles="Stamen Terrain", control=True, name="Stamen Terrain"),
    "Esri WorldImagery": folium.TileLayer(
        tiles="https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
        attr="Esri", name="ESRI Satélite", control=True
    )
}

# ------------------ Conexión Google Sheets ------------------
@st.cache_resource(show_spinner=False)
def _get_gs_client():
    creds = Credentials.from_service_account_info(
        dict(st.secrets["gcp_service_account"]),
        scopes=["https://www.googleapis.com/auth/spreadsheets",
                "https://www.googleapis.com/auth/drive"]
    )
    return gspread.authorize(creds)

def _open_or_create_worksheet(gc):
    sh = gc.open_by_key(SHEET_ID)
    try:
        ws = sh.worksheet(WS_NAME)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=WS_NAME, rows=1000, cols=len(HEADERS))
        ws.append_row(HEADERS)
    # Asegurar encabezados
    values = ws.row_values(1)
    if [h.strip().lower() for h in values] != [h.lower() for h in HEADERS]:
        ws.resize(rows=max(2, ws.row_count), cols=len(HEADERS))
        ws.update(f"A1:{chr(64+len(HEADERS))}1", [HEADERS])
    return ws

def _read_df(ws) -> pd.DataFrame:
    data = ws.get_all_records()
    df = pd.DataFrame(data)
    if df.empty:
        df = pd.DataFrame(columns=HEADERS)
    # Tipos
    for col in ["lat","lon"]:
        if col in df.columns: df[col] = pd.to_numeric(df[col], errors="coerce")
    if "fecha_evento" in df.columns:
        df["fecha_evento"] = pd.to_datetime(df["fecha_evento"], errors="coerce").dt.date
    if "timestamp" in df.columns:
        df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
    # Ordenar columnas
    for h in HEADERS:
        if h not in df.columns:
            df[h] = np.nan
    return df[HEADERS].copy()

def _append_row(ws, record: dict):
    row = [record.get(h,"") for h in HEADERS]
    ws.append_row(row)

def _find_row_index_by_id(ws, _id: str):
    col = ws.col_values(1)
    for i, val in enumerate(col, start=1):
        if val == _id:
            return i
    return None

def _update_row_by_id(ws, _id: str, new_record: dict):
    idx = _find_row_index_by_id(ws, _id)
    if not idx:
        return False
    ws.update(f"A{idx}:{chr(64+len(HEADERS))}{idx}", [[new_record.get(h,"") for h in HEADERS]])
    return True

def _delete_row_by_id(ws, _id: str):
    idx = _find_row_index_by_id(ws, _id)
    if not idx:
        return False
    ws.delete_rows(idx)
    return True

# ------------------ Utilidades UI ------------------
def _color_for_category(cat: str, palette: dict):
    if cat in palette: return palette[cat]
    # generar color estable
    h = abs(hash(cat)) % 360
    return f"hsl({h},70%,45%)"

def _parse_etiquetas(x: str):
    if not x: return []
    return [t.strip() for t in re.split(r"[;,]", str(x)) if t.strip()]

def _weight_from_impacto(imp: str):
    return {"Alto": 1.0, "Medio": 0.6, "Bajo": 0.3}.get(str(imp), 0.5)

def _month_floor(d: date):
    if pd.isna(d): return None
    return date(d.year, d.month, 1)

# ------------------ Sidebar ------------------
with st.sidebar:
    st.header("⚙️ Configuración")
    st.caption("Fuente de datos: Google Sheets")
    st.write(f"Hoja: `{WS_NAME}`")

    # Paleta personalizada por categoría
    st.subheader("🎨 Paleta de categorías")
    if "palette" not in st.session_state:
        st.session_state.palette = {c: None for c in DEFAULT_CATEGORIAS}
    for c in DEFAULT_CATEGORIAS:
        st.session_state.palette[c] = st.color_picker(f"{c}", st.session_state.palette.get(c) or "#1f77b4", key=f"palette_{c}")

# ------------------ Datos ------------------
gc = _get_gs_client()
ws = _open_or_create_worksheet(gc)
df = _read_df(ws)

# ------------------ Filtros globales ------------------
st.sidebar.subheader("🔎 Filtros")
min_date = date(2020,1,1)
max_date = date.today()

if not df["fecha_evento"].dropna().empty:
    min_date = min(min_date, df["fecha_evento"].dropna().min())
    max_date = max(max_date, df["fecha_evento"].dropna().max())

rango_fecha = st.sidebar.date_input("Rango de fechas", (min_date, max_date))
f_prov   = st.sidebar.multiselect("Provincia", sorted([x for x in df["provincia"].dropna().unique()]))
f_canton = st.sidebar.multiselect("Cantón",    sorted([x for x in df["canton"].dropna().unique()]))
f_cat    = st.sidebar.multiselect("Categoría", sorted([x for x in df["categoria"].dropna().unique()] or DEFAULT_CATEGORIAS))
f_imp    = st.sidebar.multiselect("Impacto",   DEFAULT_IMPACTO)
f_estado = st.sidebar.multiselect("Estado",    DEFAULT_ESTADO or ["Activo"])
texto    = st.sidebar.text_input("Buscar texto (título/descr./etiquetas)")

def _apply_filters(df0: pd.DataFrame) -> pd.DataFrame:
    dff = df0.copy()
    if isinstance(rango_fecha, tuple) and len(rango_fecha)==2:
        ini, fin = rango_fecha
        dff = dff[(dff["fecha_evento"].isna()) | ((dff["fecha_evento"] >= ini) & (dff["fecha_evento"] <= fin))]
    if f_prov:   dff = dff[dff["provincia"].isin(f_prov)]
    if f_canton: dff = dff[dff["canton"].isin(f_canton)]
    if f_cat:    dff = dff[dff["categoria"].isin(f_cat)]
    if f_imp:    dff = dff[dff["impacto"].isin(f_imp)]
    if f_estado: dff = dff[dff["estado"].isin(f_estado)]
    if texto:
        patt = re.compile(re.escape(texto), re.IGNORECASE)
        dff = dff[dff[["titulo","descripcion","etiquetas"]].astype(str).apply(lambda r: any(patt.search(x) for x in r), axis=1)]
    return dff

# ------------------ Tabs ------------------
tab_reg, tab_map, tab_charts, tab_export = st.tabs(["📝 Registrar / Admin", "🗺️ Mapa", "📈 Gráficas", "⬇️ Exportar"])

# ------------------ Tab: Registrar / Admin ------------------
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
        etiquetas = st.text_input("Etiquetas (separadas por coma)")
        evidencia_url = st.text_input("URL de evidencia (foto/video/documento)")
        estado = st.selectbox("Estado", DEFAULT_ESTADO, index=0)
    with cols[3]:
        st.markdown("**Ubicación (click en el mapa o escriba coord.)**")
        lat = st.number_input("Latitud", value=float(CR_CENTER[0]), format="%.6f")
        lon = st.number_input("Longitud", value=float(CR_CENTER[1]), format="%.6f")

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
                st.experimental_rerun()

    st.divider()
    st.subheader("Administrar registros")
    dff = _apply_filters(df)
    if dff.empty:
        st.info("No hay registros que cumplan los filtros.")
    else:
        st.dataframe(dff, height=280, use_container_width=True)

        st.markdown("**Editar / Eliminar**")
        selected_id = st.selectbox("Seleccione el ID a editar", [""] + dff["id"].tolist())
        if selected_id:
            rec = dff[dff["id"]==selected_id].iloc[0].to_dict()
            et1, et2 = st.columns([1,1])
            with et1:
                new_titulo = st.text_input("Título", rec["titulo"])
                new_categoria = st.selectbox("Categoría", sorted(set(DEFAULT_CATEGORIAS + list(df["categoria"].dropna().unique()))), index=sorted(set(DEFAULT_CATEGORIAS + list(df["categoria"].dropna().unique()))).index(rec["categoria"]) if rec["categoria"] else 0)
                new_impacto = st.selectbox("Impacto", DEFAULT_IMPACTO, index=DEFAULT_IMPACTO.index(rec["impacto"]) if rec["impacto"] in DEFAULT_IMPACTO else 1)
                new_fecha = st.date_input("Fecha", rec["fecha_evento"] or date.today())
            with et2:
                new_estado = st.selectbox("Estado", DEFAULT_ESTADO, index=DEFAULT_ESTADO.index(rec["estado"]) if rec["estado"] in DEFAULT_ESTADO else 0)
                new_lat = st.number_input("Lat", value=float(rec["lat"]) if not pd.isna(rec["lat"]) else CR_CENTER[0], format="%.6f")
                new_lon = st.number_input("Lon", value=float(rec["lon"]) if not pd.isna(rec["lon"]) else CR_CENTER[1], format="%.6f")

            new_desc = st.text_area("Descripción", rec["descripcion"] or "", height=120)
            ecols = st.columns(3)
            with ecols[0]:
                if st.button("💾 Guardar cambios", use_container_width=True):
                    rec["titulo"] = new_titulo
                    rec["categoria"] = new_categoria
                    rec["impacto"] = new_impacto
                    rec["fecha_evento"] = new_fecha.isoformat()
                    rec["estado"] = new_estado
                    rec["lat"] = new_lat
                    rec["lon"] = new_lon
                    rec["descripcion"] = new_desc
                    ok = _update_row_by_id(ws, rec["id"], rec)
                    if ok:
                        st.success("Actualizado ✅")
                        st.experimental_rerun()
                    else:
                        st.error("No se encontró el registro para actualizar.")
            with ecols[1]:
                if st.button("🗑️ Eliminar", use_container_width=True):
                    if _delete_row_by_id(ws, rec["id"]):
                        st.warning("Registro eliminado.")
                        st.experimental_rerun()
                    else:
                        st.error("No se pudo eliminar.")
            with ecols[2]:
                st.caption("Edite lo necesario y luego guarde.")

# ------------------ Tab: Mapa ------------------
with tab_map:
    st.subheader("Mapa de Casos de Éxito – Costa Rica")

    left, right = st.columns([1,2])
    with left:
        zoom = st.slider("Zoom inicial", 5, 12, 7)
        base_choice = st.selectbox("Mapa base", list(BASEMAPS.keys()), index=1)
        use_cluster = st.checkbox("Agrupar marcadores (Cluster)", value=True)
        show_heat = st.checkbox("Capa Heatmap", value=True)
        # Capa de áreas por GeoJSON (URL o archivo)
        st.markdown("**Capa de áreas (GeoJSON provincias/cantones – opcional)**")
        geojson_url = st.text_input("URL GeoJSON (opcional)", value="")
        geojson_file = st.file_uploader("o sube un .geojson / .json", type=["geojson","json"])
        choropleth_on = st.checkbox("Mostrar coropleta por conteo", value=False)
        color_metric = st.selectbox("Métrica para color", ["conteo (por área)","impacto promedio"])
        st.caption("Si no proporcionas un GeoJSON, la coropleta no se mostrará.")

    dff = _apply_filters(df)
    with right:
        m = folium.Map(location=CR_CENTER, zoom_start=zoom, control_scale=True)
        # basemap
        BASEMAPS[base_choice].add_to(m)

        # Marcadores
        points = dff.dropna(subset=["lat","lon"])
        if use_cluster:
            cluster = MarkerCluster(name="Casos (cluster)")
            cluster.add_to(m)
        for _, r in points.iterrows():
            color = _color_for_category(r["categoria"], st.session_state.palette)
            popup = folium.Popup(
                html=f"<b>{r['titulo']}</b><br>{r.get('descripcion','')[:300]}<br>"
                     f"<i>{r['categoria']} • {r['impacto']} • {r['fecha_evento']}</i><br>"
                     f"{r.get('provincia','')} / {r.get('canton','')} / {r.get('distrito','')}<br>"
                     f"{'📎 <a href=\"'+r['evidencia_url']+'\" target=\"_blank\">Evidencia</a>' if r.get('evidencia_url') else ''}",
                max_width=350
            )
            marker = folium.CircleMarker(
                location=(r["lat"], r["lon"]),
                radius=8,
                color=color, fill=True, fill_color=color, fill_opacity=0.8,
                tooltip=r["titulo"]
            )
            marker.add_child(popup)
            if use_cluster: marker.add_to(cluster)
            else: marker.add_to(m)

        # Heatmap
        if show_heat and not points.empty:
            heat_data = [[row["lat"], row["lon"], _weight_from_impacto(row["impacto"])] for _,row in points.iterrows()]
            HeatMap(heat_data, name="Heatmap", radius=20, blur=15, max_zoom=12).add_to(m)

        # Coropleta (si hay GeoJSON)
        gj_obj = None
        if geojson_file is not None:
            gj_obj = json.load(geojson_file)
        elif geojson_url.strip():
            try:
                import requests
                gj_obj = requests.get(geojson_url, timeout=10).json()
            except Exception:
                st.warning("No se pudo cargar el GeoJSON desde la URL proporcionada.")

        if choropleth_on and gj_obj:
            # Determinar clave de nombre de área (común: 'name', 'NOM_PROV', 'provincia', etc.)
            # Estrategia: probar varias claves en features[0]['properties']
            props = gj_obj.get("features", [{}])[0].get("properties", {})
            candidate_keys = ["name","NOM_PROV","provincia","PROVINCIA","NOM_CANT","canton","CANTON"]
            area_key = next((k for k in candidate_keys if k in props), None)

            if not area_key:
                st.warning("No se detectó la columna de nombre de área en el GeoJSON (probadas: name, NOM_PROV, provincia, PROVINCIA, NOM_CANT, canton, CANTON). Se omitirá la coropleta.")
            else:
                # Agregar columna 'area' al df basado en provincia o cantón según coincida mejor
                if area_key.lower().startswith(("nom_cant","canton")):
                    area_df = dff.copy()
                    area_df["area"] = area_df["canton"].fillna("")
                else:
                    area_df = dff.copy()
                    area_df["area"] = area_df["provincia"].fillna("")

                # Métrica
                if color_metric.startswith("impacto"):
                    map_weight = area_df.groupby("area")["impacto"].apply(lambda s: np.mean([_weight_from_impacto(x) for x in s]))
                else:
                    map_weight = area_df.groupby("area")["id"].count()

                map_df = map_weight.reset_index().rename(columns={0:"valor","id":"valor"})
                map_df["area_norm"] = map_df["area"].str.strip().str.lower()

                # Vincular
                style_function = lambda x: {"fillColor": "#eeeeee", "color":"#555", "weight":1, "fillOpacity":0.6}
                folium.GeoJson(
                    gj_obj,
                    name="Áreas",
                    style_function=style_function,
                    highlight_function=lambda x: {"weight":2, "color":"#222"},
                    tooltip=folium.GeoJsonTooltip(fields=[area_key], aliases=["Área"])
                ).add_to(m)

                # Pintar según valor
                from branca.colormap import linear
                if len(map_df):
                    vmin, vmax = float(map_df["valor"].min()), float(map_df["valor"].max())
                    cmap = linear.YlOrRd_09.scale(vmin, vmax)
                    def choropleth_style(feature):
                        name_val = str(feature["properties"].get(area_key,"")).strip().lower()
                        row = map_df[map_df["area_norm"]==name_val]
                        if not row.empty:
                            val = float(row["valor"].values[0])
                            return {"fillColor": cmap(val), "color":"#444", "weight":1, "fillOpacity":0.7}
                        else:
                            return {"fillColor": "#dddddd", "color":"#444", "weight":1, "fillOpacity":0.3}
                    folium.GeoJson(
                        gj_obj,
                        name="Coropleta",
                        style_function=choropleth_style
                    ).add_to(m)
                    cmap.caption = "Intensidad por área"
                    cmap.add_to(m)

        folium.LayerControl(collapsed=False).add_to(m)
        st_folium(m, use_container_width=True, height=650)

# ------------------ Tab: Gráficas ------------------
with tab_charts:
    st.subheader("Análisis y Gráficas")
    dff = _apply_filters(df)
    if dff.empty:
        st.info("No hay datos para graficar con los filtros actuales.")
    else:
        # Preparaciones
        dff["mes"] = dff["fecha_evento"].apply(_month_floor)
        # Selectores
        colA, colB, colC = st.columns([1,1,1])
        with colA:
            tipo = st.selectbox("Tipo de gráfico", ["Barras por categoría","Barras por provincia","Serie mensual","Top N cantones","Torta por categoría"])
        with colB:
            n_top = st.number_input("Top N (si aplica)", min_value=3, max_value=30, value=10, step=1)
        with colC:
            stacked = st.checkbox("Apilado (si aplica)", value=True)

        # Paleta para Altair
        cat_list = sorted(dff["categoria"].dropna().unique().tolist() or DEFAULT_CATEGORIAS)
        cat_colors = [ _color_for_category(c, st.session_state.palette) for c in cat_list ]
        alt.themes.enable('opaque')
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

# ------------------ Tab: Exportar ------------------
with tab_export:
    st.subheader("Exportar datos")
    dff = _apply_filters(df)
    st.write(f"Registros filtrados: **{len(dff)}**")

    c1, c2 = st.columns(2)
    with c1:
        csv = dff.to_csv(index=False).encode("utf-8")
        st.download_button("⬇️ Descargar CSV", data=csv, file_name="casos_exito.csv", mime="text/csv", use_container_width=True)
    with c2:
        output = io.BytesIO()
        with pd.ExcelWriter(output, engine="xlsxwriter") as writer:
            dff.to_excel(writer, index=False, sheet_name="casos")
        st.download_button("⬇️ Descargar Excel", data=output.getvalue(), file_name="casos_exito.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", use_container_width=True)

st.caption("© Casos de Éxito – Costa Rica")



