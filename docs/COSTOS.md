# Costo medido del módulo 2 y estimación para T1

Medición sobre UNA celda de prueba de 10x10 km en la costa atlántica fueguina
(estepa costera, 11 % agua, sin bosque), versión de algoritmo 0.1.3.
Máquina: 4 vCPU, 15 GB RAM, 1 worker con 4 hilos. Sin coordenadas en este archivo.

## Datos
- Catálogo S2 L2A 2019–2025: 532 fechas; 380 útiles tras el filtro SCL (>=15 % píxeles claros).
- S1 RTC: 60 escenas de una órbita relativa (submuestreo uniforme).
- Celda leída con margen de 1 km: 1200 x 1200 px a 10 m.

## Tiempo por etapa (s, reloj)
| etapa | s |
|---|---|
| búsqueda STAC S2 | 20 |
| filtro SCL de 532 fechas (20 m) | 19 |
| espera de E/S S2 (solapada con cómputo, lotes de 8) | 30 |
| rasgos por fecha | 16 |
| **residuos multiescala por fecha (reloj, 4 hilos)** | **241** |
| compuestos temporales S2 | 40 |
| S1 búsqueda + lectura + compuestos | 22 |
| descriptores (5 escalas) | 80 |
| vectorización + guardado | 51 |
| **total por celda** | **567** |

## Por escala (tiempo de CPU acumulado en hilos para las 380 fechas x 4 rasgos)
| escala | residuo por fecha | compuestos | componentes + watershed | descriptores | componentes |
|---|---|---|---|---|---|
| 50 m | 186 | 0.8 | 1.2 | 25.1 | 12 042 |
| 150 m | 157 | 0.7 | 0.9 | 17.6 | 7 750 |
| 500 m | 148 | 0.7 | 0.9 | 14.5 | 6 495 |
| 1.5 km | 140 | 0.7 | 1.0 | 12.8 | 5 327 |
| 5 km | 142 | 0.7 | 0.8 | 10.5 | 4 501 |

El costo por escala es casi constante: el fondo por bloques no crece con la ventana.

## Memoria y disco
- RSS pico 8.6 GB (incluye ~4.4 GB de páginas de memmap respaldadas en disco, reclamables).
- Caché temporal por celda: 2.2 GB (se borra al terminar).
- Productos por celda: ~173 MB (rasters de score y compuestos, series, candidatos).

## Estimación T1
- 345 celdas en T1; 239 con tierra >= 2 % (las de mar abierto terminan en segundos como `water`).
- 1 worker: 239 x 567 s ≈ **37.6 h**.
- Cota inferior por CPU (~1050 CPU-s/celda en 4 núcleos): ≈ **17.4 h**.
- 2 workers (cabe en 15 GB): estimado ≈ **22 h**.
- Disco: ~41 GB de productos por celda para T1 (más que la cuota actual de 30 GB libres):
  antes de escalar conviene no guardar los 10 rasters de compuestos o guardarlos en float16.
- Candidatos: ~36 000 por celda => ~8.6 M en T1 antes del triaje.
