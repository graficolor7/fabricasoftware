# Git, secretos y seguridad

## Git es evidencia
- Commits pequeños y con sentido; nada de cambios no relacionados.
- Milestones y Supervisor Packets citan el commit exacto. Milestones estables pueden llevar tag.
- No reescribir historia compartida sin autorización. No borrar evidencia de tests fallidos porque una corrida posterior pasó.
- Contratos referencian su versión.
- Ramas: `factory/...` (fábrica), `task/<TASK_ID>` (tareas), `main` protegida.
- `main` exige CI verde. El merge de cierre de milestone y el release exigen al Product Owner.

## Secretos
- Nunca: commitear claves, registrarlas en logs, incluirlas en Supervisor Packets ni pasar `.env` a agentes sin necesidad.
- Claves solo como GitHub Secrets (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`) o variables de entorno.
- El adaptador del supervisor rechaza paquetes con patrones de secretos.

## Seguridad del producto
- Todo archivo importado es no confiable: seguridad del parser, agotamiento de recursos, archivos malformados, path traversal, referencias externas, contenido embebido, abuso de memoria.
- Parsers con fuzzing cuando corresponda.
- Permisos mínimos en workflows (`permissions:` explícito).

## CI
CI ejecuta verificaciones deterministas: compilador, lint, tests, schemas, análisis estático, seguridad. Los modelos no sustituyen a estas herramientas: la IA supervisa e interpreta, las herramientas verifican.
