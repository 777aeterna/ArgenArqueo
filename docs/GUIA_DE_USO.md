# Guía de uso

Esta guía explica, paso a paso, cómo instalar arqueo-patagonia, de dónde salen
los datos, cómo correr cada etapa y cómo leer los resultados.

## 1. ¿Hay que descargar datos?

**No.** No hay que bajar imágenes a mano ni preparar carpetas de datos. El
programa busca y lee las imágenes solo, desde internet, usando catálogos STAC
(un estándar para buscar imágenes satelitales). De cada imagen lee **solo la
ventana** que cae sobre la celda que está procesando, no la escena entera.

| dato | de dónde lo lee | para qué |
|---|---|---|
| Sentinel-2 L2A (2019–2025) | catálogo Earth Search (`earth-search.aws.element84.com`) | rasgos espectrales por fecha y compuestos |
| Sentinel-1 RTC | Microsoft Planetary Computer (`planetarycomputer.microsoft.com`) | retrodispersión de radar (mediana y varianza) |
| Copernicus DEM GLO-30 | Earth Search | prior de preservación, contexto topográfico, paredones |
| ESA WorldCover 2021 | bucket público de ESA en AWS | máscara de bosque y agua |
| Hansen Global Forest Change | Google Cloud Storage público | máscara de bosque (complemento) |
| OpenStreetMap | servidores Overpass | marcar infraestructura moderna |
| Satellogic EarthView (1 m) | bucket público en AWS | recortes de alta resolución para las fichas |
| CORONA/HEXAGON (opcional) | API M2M del USGS | imágenes históricas 1960–1972 |

Lo único que **sí** carga el usuario a mano es, opcionalmente, la lista de
sitios publicados para la validación (sección 7).

### Requisitos de red

La máquina necesita salida a internet hacia esos servidores. Si trabajás detrás
de un firewall o proxy, tienen que estar permitidos al menos:
`earth-search.aws.element84.com`, `sentinel-cogs.s3.us-west-2.amazonaws.com`,
`copernicus-dem-30m.s3.amazonaws.com`, `planetarycomputer.microsoft.com`,
`*.blob.core.windows.net` (Sentinel-1), `esa-worldcover.s3.eu-central-1.amazonaws.com`,
`storage.googleapis.com` y un servidor Overpass (`overpass-api.de` u otro espejo;
ver `OVERPASS_MIRRORS` en `arqueo/config.py`).

## 2. Requisitos de la máquina

| recurso | mínimo | recomendado |
|---|---|---|
| sistema | Linux (probado en Python 3.13) | Linux Mint / Ubuntu |
| CPU | 4 núcleos | 8+ núcleos |
| RAM | 16 GB (1 worker) | 32 GB (2 workers) |
| disco | ~100 MB por celda procesada + 2,2 GB temporales por worker | SSD |
| tiempo | ~10 min por celda de 10×10 km con 4 núcleos | |

## 3. Instalación

```bash
git clone https://github.com/777aeterna/ArgenArqueo.git
cd ArgenArqueo
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
pip install bottleneck pysheds pytest     # aceleran el cálculo y habilitan el análisis hidrológico
python -m pytest -q tests                 # todo debe pasar
arqueo --help
```

Si `arqueo` no aparece como comando, usá `python -m arqueo.cli` en su lugar.

## 4. Cómo está organizado el trabajo

- El territorio se divide en **celdas de 10×10 km** agrupadas en cuatro
  **tiers** (T1 costas fueguinas, T2 costa de Santa Cruz, T3 mesetas, T4
  cañadones). Las áreas de cada tier están en `TIERS`, dentro de
  `arqueo/grid.py`; para procesar otra zona, agregala ahí.
- Cada celda tiene un id como `19S_550_4040`: zona UTM, y coordenadas este y
  norte en kilómetros de su esquina inferior izquierda.
- Todo el avance se guarda en una base SQLite (`data/state.sqlite`). Si el
  proceso se corta, se vuelve a lanzar el mismo comando y sigue donde quedó.
- Si cambiás un parámetro de `arqueo/config.py`, cambia el "hash de
  parámetros" y las celdas ya procesadas se rehacen automáticamente.

## 5. Flujo de trabajo, paso a paso

### Paso 1 — Registrar las celdas

```bash
arqueo init-grid                # todos los tiers
arqueo init-grid --tiers T1     # solo uno
```

Crea `data/state.sqlite` con una fila por celda, en estado `pending`.

### Paso 2 — Calcular el prior de preservación (opcional pero recomendado)

```bash
arqueo prior --tier T1 --workers 3
```

Para cada celda lee el DEM y la cobertura, y estima dónde algo antiguo pudo
conservarse (huella glacial, tefra, sedimentación activa, superficies
estables). Tarda ~7 s por celda. **Solo cambia el orden** en que se procesan
las celdas; nunca excluye ninguna.

```bash
arqueo rank                     # muestra la cola resultante
arqueo rank --escarpments       # T4: ranquea segmentos de paredón (outputs/escarpments.gpkg)
```

### Paso 3 — Correr el detector

```bash
arqueo run --tier T1 --limit 5 --workers 1          # las 5 primeras celdas de la cola de T1
arqueo run --cells 19S_550_4040 19S_560_4040        # celdas puntuales
arqueo run --cells 19S_550_4040 --force             # reprocesar aunque ya esté hecha
```

Por cada celda: busca ~530 fechas de Sentinel-2, descarta las nubladas
(quedan ~380), calcula rasgos por fecha, los compara contra su entorno a 5
escalas, mide en cuántas fechas la diferencia se repite, extrae las regiones
anómalas y calcula sus descriptores. Luego agrega Sentinel-1.

Consejos:

- Empezá con `--limit` chico para medir el tiempo en tu máquina.
- `--workers 2` solo si tenés 32 GB de RAM.
- `ARQUEO_THREADS=8 arqueo run ...` para usar más núcleos dentro de cada celda.
- Para dejarlo corriendo aunque cierres la terminal:
  `nohup arqueo run --tier T1 > outputs/run.log 2>&1 &`

### Paso 4 — Ver el avance

```bash
arqueo status              # celdas por estado y candidatos encontrados
arqueo status --timings    # tiempo por etapa y por escala de cada celda
```

Estados posibles: `pending`, `running`, `done`, `error`, `water` (mar abierto)
y `forest_gap` (bosque cerrado: no evaluable sin LiDAR; es una brecha de
cobertura, no una zona sin hallazgos).

### Paso 5 — Triaje

```bash
arqueo triage
```

Une los candidatos de todas las celdas procesadas y:

1. consulta OpenStreetMap y **marca** (no borra) lo que coincide con caminos,
   edificios, alambrados, pozos petroleros, etc.;
2. calcula la **rareza** de cada candidato contra los demás candidatos del
   mismo paisaje;
3. los ordena en tres grupos: `NO_CLASIFICABLE` (primero), `morfologia_descrita`
   y `moderno_historico`;
4. marca como **restringidos** los posibles chenques.

Conviene correr el triaje cuando haya varias celdas: la rareza se mide contra
la población del paisaje, y con una sola celda esa población es chica.

### Paso 6 — Fichas, export y demo

```bash
arqueo chips --top 50                 # fichas PNG + JSON en outputs/chips/
arqueo chips --top 50 --no-satellogic # más rápido, sin buscar recortes de 1 m
arqueo export --top 500               # outputs/export/candidatos_export.gpkg (sin restringidos)
arqueo demo --dest ejemplo            # versión pública SIN georreferencia (ver ejemplo/)
```

## 6. Qué produce y cómo leerlo

Todo queda en `outputs/` (fuera del repositorio):

| archivo | contenido |
|---|---|
| `outputs/candidates.gpkg` | **todos** los candidatos, con geometría, descriptores, scores, bucket, rareza y `rank`. Se abre en QGIS |
| `outputs/cells/<celda>/score_<escala>.tif` | mapa de anomalía por escala (≥ 1 = anómalo) |
| `outputs/cells/<celda>/comp_*.tif` | compuestos temporales (NDVI, brillo, razones, radar) |
| `outputs/cells/<celda>/rgb_median.tif` | imagen color de referencia |
| `outputs/cells/<celda>/timeseries.npz` | serie temporal del píxel pico de cada candidato |
| `outputs/chips/` | fichas; las restringidas en `outputs/chips/RESTRINGIDO/` |
| `outputs/export/` | export sin restringidos |
| `outputs/validation_report.json` | resultado de la validación |

### Columnas principales del GeoPackage

| columna | significado |
|---|---|
| `rank` | posición en el ranking final (1 = revisar primero) |
| `bucket` | `NO_CLASIFICABLE`, `morfologia_descrita` o `moderno_historico` |
| `literature_class` | etiqueta bibliográfica gruesa, si alguna regla coincide |
| `restricted` | `True` = posible chenque: no se exporta nunca |
| `rarity` | 0–1, qué tan inusual es frente a su paisaje |
| `scale_m` | escala en la que se detectó (50, 150, 500, 1500, 5000 m) |
| `n_scales` | en cuántas escalas aparece |
| `dominant_feature` | rasgo que más se aparta (ndvi, bright, r1112, r42, s1vv_var, …) |
| `sign` | +1 más alto que el entorno, −1 más bajo |
| `persist_z` | cuánto supera la persistencia a lo esperable por azar |
| `years_hit` | en cuántos de los 7 años aparece la anomalía |
| `area_m2`, `eccentricity`, `compactness`, `edge_straightness`, `symmetry_*` | forma |
| `modern_tags` | elementos de OSM con los que coincide |

### En QGIS

1. Capa → Añadir capa vectorial → `outputs/candidates.gpkg`.
2. Simbología por categorías sobre `bucket`; filtro, por ejemplo,
   `"rank" <= 200 AND "restricted" = false`.
3. Agregá debajo los rasters `score_*.tif` y `rgb_median.tif` de la celda.

## 7. Cargar sitios publicados para la validación

1. Copiá la plantilla:
   `cp config/sitios_publicados.template.csv data/validation/sitios_publicados.csv`
2. Completá una fila por sitio **publicado**, con su cita:

   ```
   site_id,name,lat,lon,precision_m,province,site_type,reference
   S001,Nombre del sitio,-53.123,-68.456,500,Tierra del Fuego,conchero,"Autor (año), Revista, pág."
   ```

   - `lat` y `lon` en grados decimales (WGS84, negativos al sur y al oeste).
   - `precision_m`: incertidumbre de la ubicación publicada (por ejemplo, 1000 si
     se ubicó a partir de un mapa del artículo).
   - `reference` es obligatoria.
   - No incluyas entierros ni sitios cuya ubicación esté protegida.
3. Corré `arqueo validate`.

El archivo vive en `data/`, que nunca se sube al repositorio. La validación es
un **control de cordura**: reporta recall@N, el resultado de un modelo nulo con
puntos al azar y el lift. **No ajustes parámetros para mejorar esos números**;
si el recall es cero en todos los N, hay un error que corregir.

## 8. Credenciales opcionales

- **CORONA/HEXAGON:** creá una cuenta en ERS del USGS, generá un token M2M y
  exportá `USGS_M2M_USERNAME` y `USGS_M2M_TOKEN` antes de correr `arqueo chips`.
  Sin ellas, esa parte se saltea sola.
- Ningún otro dato requiere credenciales.

## 9. Problemas frecuentes

| síntoma | causa probable | qué hacer |
|---|---|---|
| `Overpass no respondió` | servidor saturado o bloqueado | reintentar; cambiar `ARQUEO_OVERPASS` a otro espejo. Sin OSM el triaje sigue, sin marcar lo moderno |
| celda en `error` con "solo N fechas útiles" | nubosidad extrema o celda casi toda agua | normal en algunas celdas; queda registrada |
| se queda sin memoria | demasiados workers | bajar `--workers` a 1 |
| `No space left on device` | disco lleno | ~100 MB por celda + 2,2 GB temporales por worker |
| errores de red durante `run` | cortes de conexión | volver a lanzar: sigue donde quedó |
| recall 0 en `validate` | sitios fuera de las celdas procesadas, o coordenadas mal cargadas | revisar `n_sites_in_processed_cells` en el reporte |
