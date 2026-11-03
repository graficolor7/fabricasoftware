# Aplicar la fábrica a un proyecto

## Proyecto existente
1. **G0 Discovery (solo lectura):** reconstruir el estado real desde el repo (estado actual, arquitectura, contratos, ADRs, milestones, tests, evidencia, incertidumbres). Entregar el informe F0. No tocar nada.
2. Con aprobación del PO, en una rama `factory/bootstrap`:
   - `.factory/PROJECT_STATE.json` (valida con `python tools/validate.py project-state ...`).
   - `.factory/PROFILE.md`: PROJECT_ID, tipo, plataforma, dominios, arquitectura, contratos, verificación por dominio, agentes específicos, zonas exclusivas, milestones, riesgos, gates propios.
   - `CLAUDE.md` en la raíz: reglas para los agentes, apuntando a lo anterior.
   - `.github/workflows/ci.yml` con las verificaciones deterministas del proyecto.
   - `.factory/supervisor.json` (perfil del supervisor: `templates/SUPERVISOR_PROFILE.json`).
   - `.github/workflows/supervisor-gate.yml` que llama al workflow reutilizable de la fábrica (ver `docs/supervisor/README.md`).
   - Primera revisión: `factory supervisor baseline` (BASELINE_SUPERVISOR_REVIEW).
   - Un ADR que registre la adopción de la fábrica.
3. Respetar la nomenclatura existente del proyecto; se mapea, no se renombra.

## Proyecto nuevo
Igual, pero G0 es corto y se pasa a G1 (requisitos) y G2 (arquitectura con spikes) antes de implementar.

## Secretos y variables en el repo del proyecto
- Secret `OPENAI_API_KEY`; variables `SUPERVISOR_MODEL` (o `OPENAI_MODEL`), `SUPERVISOR_PROVIDER=openai` y `SUPERVISOR_LIVE_ENABLED=true` solo cuando el PO autorice el consumo real.
- Secret `ANTHROPIC_API_KEY` si se usa Claude Code en GitHub Actions.
