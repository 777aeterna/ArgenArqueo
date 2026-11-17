# arqueo-patagonia

Prospección arqueológica remota en la Patagonia austral por **detección de
anomalías sin plantilla**, con datos abiertos.

## Principio rector

El objetivo NO es encontrar sitios parecidos a los ya conocidos. Un detector
construido alrededor de morfologías conocidas solo halla morfologías conocidas
(sesgo del instrumento). Por eso:

- toda la detección es **no supervisada y multiescala** (50 m, 150 m, 500 m,
  1.5 km, 5 km; todas las escalas corren siempre);
- se buscan píxeles y regiones que **persisten** estadísticamente distintos de
  su entorno local, sin presuponer forma ni tamaño;
- lo que no encaja en ninguna clase conocida se ranquea **ALTO** (bucket
  `NO_CLASIFICABLE` primero), no se descarta;
- los sitios publicados solo sirven como **control de cordura**; está prohibido
  ajustar hiperparámetros para maximizar el recall sobre ellos.

## Módulos

| módulo | archivo | qué hace |
|---|---|---|
| grilla | `arqueo/grid.py` | celdas 10x10 km en UTM sur, tiers T1–T4, orden sur→norte |
| estado | `arqueo/state.py` | SQLite: una fila por celda (status, hash de parámetros, timestamp); reanudable y paralelizable |
| datos | `arqueo/stac.py` | STAC + ventanas COG: S2 L2A (Earth Search), S1 RTC (Planetary Computer), Copernicus DEM, WorldCover, Hansen |
| 1 prior | `arqueo/prior.py`, `arqueo/escarpments.py` | prior de preservación (solo ordena la cola); ranking T4 de paredones |
| 2 detector | `arqueo/anomaly.py`, `multiscale.py`, `features.py`, `descriptors.py` | residuos robustos multiescala, persistencia binomial, componentes y descriptores |
| 3 triaje | `arqueo/modern.py`, `arqueo/triage.py` | OSM (marca, no borra), rareza (IsolationForest + MCD), 3 buckets, clase restringida |
| 4 validación | `arqueo/validate.py` | recall@N, nulo con el mismo prior, lift; números crudos |
| salida | `arqueo/chips.py`, `export.py`, `satellogic.py`, `corona.py` | fichas, export sin restringidos, Satellogic 1 m, CORONA/HEXAGON |

## Uso

```bash
pip install -e .
arqueo init-grid                     # registra las celdas de T1..T4
arqueo prior --tier T1 --workers 3   # métricas de preservación -> ordena la cola
arqueo rank                          # muestra la cola; --escarpments para T4
arqueo run --tier T1 --workers 2 --limit 10
arqueo status --timings
arqueo triage                        # OSM + rareza + buckets
arqueo validate                      # requiere data/validation/sitios_publicados.csv
arqueo chips --top 50
arqueo export --top 500              # NUNCA incluye la clase restringida
```

Variables opcionales: `ARQUEO_THREADS` (hilos por worker), `ARQUEO_OVERPASS`,
`USGS_M2M_USERNAME` + `USGS_M2M_TOKEN` (CORONA/HEXAGON).

## Resguardo

- `.gitignore` excluye todo producto con coordenadas: `data/`, `outputs/`,
  `*.gpkg`, `*.tif`, `*.csv` (salvo plantillas sin datos). Al repo va solo código.
- Posibles chenques (entierros) → `restricted=True`: quedan en el archivo local,
  sus fichas van a `outputs/chips/RESTRINGIDO/` y **no salen en ningún export**.
- El bosque de dosel cerrado (WorldCover 10 ∪ Hansen ≥ 60 %) se enmascara y
  queda registrado como brecha de cobertura (`forest_frac`, status
  `forest_gap`): "no evaluable sin LiDAR", no "zona sin hallazgos".

## Proxies y limitaciones declaradas

- Límites del UMG: sin vectorial accesible, proxies en DEM (depresiones
  cerradas, relieve hummocky, lineación). Tefra: índice por distancia a centros
  eruptivos con sesgo E-SE. Litología (T4): proxy de roca de coronamiento.
- El fondo local usa mediana por bloques en dos grillas desfasadas e
  interpolación bilineal (aproximación eficiente del filtro de mediana).
- Margen de lectura de 1 km: la escala de 5 km tiene efecto de borde atenuado.
- A 10 m, estructuras de pocos metros (chenques, parapetos) son sub-píxel:
  los recortes Satellogic de 1 m sobre los mejores candidatos son el control.
- Las reglas de "morfología descrita" del triaje son etiquetas, no detectores.
