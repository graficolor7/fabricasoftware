# fabricasoftware

Protocolo universal de la fábrica de software de Graficolor: **Claude construye, ChatGPT supervisa, las herramientas verifican y el Product Owner decide.**

- Empieza por [`FACTORY.md`](FACTORY.md) (constitución) y [`docs/INIT_NEW_PROJECT.md`](docs/INIT_NEW_PROJECT.md).
- Reglas en [`policies/`](policies/), contratos de datos en [`schemas/`](schemas/), prompts en [`prompts/`](prompts/).
- El gate del supervisor: [`adapters/supervisor/supervisor.py`](adapters/supervisor/supervisor.py) y el workflow reutilizable [`.github/workflows/supervisor-gate.yml`](.github/workflows/supervisor-gate.yml).

Versión: ver [`VERSION`](VERSION). Proyectos que la usan: GrafiArts (editor vectorial).

## Desarrollo local
```
pip install -r requirements.txt
python -m pytest -q
python tools/validate.py supervisor-packet ruta/al/paquete.json
```

## Probar el supervisor a mano
```
export OPENAI_API_KEY=...   # nunca en el repo
export OPENAI_MODEL=...     # el modelo que elija el Product Owner
python adapters/supervisor/supervisor.py --packet templates/SUPERVISOR_PACKET.json --out v.json --md v.md
```
