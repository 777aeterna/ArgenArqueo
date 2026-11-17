# Ejemplo: bloque de 30×30 km en la costa atlántica fueguina

Resultado real del pipeline sobre **9 celdas de 10×10 km** (bloque de 3×3) en
la estepa costera al norte de Río Grande, Tierra del Fuego. Sirve para ver
**cómo trabaja** el detector. Está publicado **sin georreferencia**.

## Qué hay acá

| archivo | contenido |
|---|---|
| `resumen.json` | conteos de candidatos por escala y por grupo, fechas útiles y tiempo de cómputo por celda |
| `candidatos_top.csv` | los 500 primeros del ranking, con sus descriptores y scores |
| `fichas/cand_000NN.png` | 30 fichas: recorte Sentinel-2 de 500 m, mapas de anomalía en las 5 escalas (ventana de 1,5 km), serie temporal 2019–2025 del píxel pico y descriptores |

## Cifras del bloque

- 9 celdas, ~380 fechas útiles de Sentinel-2 por celda (2019–2025), ~9,5 min por celda.
- **373 146 candidatos** en total: 114 493 a 50 m, 78 871 a 150 m, 68 455 a 500 m,
  58 265 a 1,5 km y 53 062 a 5 km.
- Grupos: 319 153 `NO_CLASIFICABLE`, 40 530 `morfologia_descrita`,
  13 463 `moderno_historico` (coinciden con OpenStreetMap).
- **2 611 restringidos** (posibles chenques): **no se publican**.

## Cómo leer una ficha

- **Arriba a la izquierda:** mediana de color real de Sentinel-2, 500 m de lado.
- **Mapas de score:** cuánto se aparta cada píxel de su entorno a cada escala
  (claro = más anómalo; ≥ 1 en unidades de umbral = anómalo). Que un rasgo
  aparezca en las cinco escalas indica que no es ruido de una sola ventana.
- **Serie temporal:** valores por fecha de NDVI, brillo y razones de suelo en
  el píxel más anómalo. Una anomalía real se repite año tras año.
- **Descriptores:** forma, persistencia (`persist_z`, `years_hit`), rareza y grupo.

## Coordenadas

`x_rel_m` e `y_rel_m` son **coordenadas locales en metros**: el origen está en
el centro del conjunto y los ejes tienen una **rotación secreta**. Las
distancias entre candidatos son reales; la orientación y la ubicación no.

## Advertencias honestas

- **Un candidato no es un sitio.** Nada de esto fue verificado en el campo ni
  revisado por arqueólogos.
- **Muchos de los primeros del ranking son rasgos naturales**, por ejemplo
  cauces de arroyos (la ficha `cand_00001` es uno): el triaje marca la
  infraestructura moderna de OSM, pero todavía no los cursos de agua naturales.
- Generado con la versión de algoritmo **0.1.3**. Esa versión tenía un error
  de NDVI con reflectancias casi nulas (valores fuera de [-1, 1] en algunas
  series), corregido en la 0.1.4.
