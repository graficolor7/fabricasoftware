# fabricasoftware

Protocolo universal de la fábrica de software de Graficolor: **Claude construye, ChatGPT supervisa, las herramientas verifican y el Product Owner decide.**

- Empieza por [`FACTORY.md`](FACTORY.md) (constitución) y [`docs/INIT_NEW_PROJECT.md`](docs/INIT_NEW_PROJECT.md).
- Reglas en [`policies/`](policies/), contratos de datos en [`schemas/`](schemas/), prompts en [`prompts/`](prompts/).
- Supervisor automático: [`docs/supervisor/README.md`](docs/supervisor/README.md) (`python -m factory …`) y el workflow reutilizable [`.github/workflows/supervisor-gate.yml`](.github/workflows/supervisor-gate.yml).

Versión: ver [`VERSION`](VERSION). Proyectos que la usan: GrafiArts (editor vectorial).

## Desarrollo local
```
pip install -r requirements.txt
python -m pytest -q                      # sin red: el supervisor real nunca se llama en tests
python tools/validate.py supervisor-packet ruta/al/paquete.json
```

## Usar el supervisor en un proyecto
```
export PYTHONPATH=/ruta/a/fabricasoftware
python -m factory --project-root RUTA status
python -m factory --project-root RUTA supervisor review --draft borrador.json --milestone M1 --gate G6 --trigger MILESTONE_COMPLETION
```
Por defecto el proveedor es `mock` y las llamadas reales están deshabilitadas (`SUPERVISOR_LIVE_ENABLED=false`). La clave va solo en `OPENAI_API_KEY` (ver `.env.example`).
