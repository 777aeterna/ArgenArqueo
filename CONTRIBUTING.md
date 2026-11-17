# Contribuir a arqueo-patagonia

¡Gracias por el interés! Este es un prototipo de investigación escrito por un
agente de IA (ver "Cómo se hizo" en el README); las revisiones críticas son
tan valiosas como el código nuevo.

## Reglas que no se negocian

1. **Nada con coordenadas en el repositorio.** Ni candidatos, ni rasters, ni
   sitios de validación, ni capturas de mapas. Revisá `git status` antes de
   cada commit; `.gitignore` ayuda pero no reemplaza el cuidado.
2. **Nada de optimizar contra sitios conocidos.** No se aceptan cambios cuyo
   objetivo sea subir el recall sobre sitios publicados. La validación es un
   control de cordura.
3. **La clase restringida no se exporta nunca.** Cualquier cambio en
   `export.py` o en el triaje debe mantener el test `tests/test_export.py`.

## Qué sí ayuda

- Revisión arqueológica de las reglas de triaje (`arqueo/triage.py`).
- Mejores proxies abiertos para límites glaciales, tefra o litología.
- Bajar costo de cómputo, memoria o disco por celda.
- Tests sintéticos que muestren qué detecta y qué no cada escala.

## Flujo

```bash
pip install -e . && pip install pytest bottleneck pysheds
python -m pytest -q tests
```

Abrí un issue antes de cambios grandes. Si cambiás algo que altera los
resultados, subí `ALGO_VERSION` en `arqueo/config.py`: eso invalida el hash de
parámetros y fuerza el reprocesamiento de las celdas.
