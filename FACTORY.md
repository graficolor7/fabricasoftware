# SOFTWARE FACTORY · Constitución

**FACTORY_VERSION:** ver `VERSION` (1.1.0). La versión de la fábrica es independiente de la de cada proyecto.

## Propósito
Protocolo reutilizable y permanente para desarrollar software con IA: Claude construye, un supervisor independiente (ChatGPT) audita en los gates, las herramientas deterministas verifican y el Product Owner humano decide.

## FACTORY vs PROJECT
- **FACTORY** (este repo): metodología, gobierno, roles, políticas, schemas, prompts, plantillas, adaptadores y workflows reutilizables.
- **PROJECT** (repo de cada producto): código, tests, evidencia, contratos, ADRs y su **perfil** en `.factory/`.
- Nunca se mezclan. Las políticas universales no contienen reglas de un proyecto concreto: "cada proyecto define sus dominios de verificación", no "todo software debe probar curvas Bézier".

## Principios
1. **Preservar antes que construir.** Un proyecto existente se descubre (G0) antes de tocarse. Prohibido reiniciar, borrar historia o evidencia, o cambiar contratos congelados sin Change Request.
2. **Evidencia antes que afirmaciones.** Nada es DONE ni VERIFIED sin evidencia reproducible.
3. **La memoria oficial es el repo:** Git + PROJECT_STATE + documentación + ADRs + contratos + tests + evidencia. Las conversaciones de IA no son memoria.
4. **Contexto mínimo.** El consumo de tokens es un requisito de arquitectura.
5. **Determinista primero.** CI verifica; la IA interpreta y supervisa.
6. **Sin bucles.** Reintentos con evidencia nueva, stop-loss y escalado al humano.
7. **Proveedores intercambiables.** Clases abstractas (DIRECTOR, WORKER, FAST, SUPERVISOR) y adaptadores; cambiar de modelo no cambia el protocolo.

## Roles
| Rol | Quién | Autoridad |
|---|---|---|
| Product Owner | Humano | final: alcance, releases, desacuerdos, excepciones |
| Supervisor | ChatGPT (API OpenAI) | veredicto en gates |
| Director | Claude Opus | plan, delegación, integración, paquetes |
| Workers | Claude Sonnet / Haiku y agentes especializados | tareas con contrato |
| Herramientas | compilador, tests, CI, verificadores | verificación |

Agentes **universales** (Director, Test, Code Review, Documentation, Security, Research) y **específicos del proyecto** (los declara su perfil). Se crean solo los necesarios.

## Contenido del repo
| Ruta | Qué es |
|---|---|
| `policies/` | reglas: modelos y tokens, tareas y gates, supervisor, git y seguridad |
| `schemas/` | JSON Schema de PROJECT_STATE, tarea, paquete y veredicto del supervisor, Change Request |
| `prompts/` | prompts base de Director, Worker, Fast Worker y Supervisor |
| `templates/` | plantillas para arrancar un proyecto y sus registros (incluye `github/ISSUE_TEMPLATE/tarea.yml`) |
| `factory/` | herramientas: `python -m factory supervisor …` (SupervisorAdapter OpenAI/Mock/Manual, paquetes, evidencia, política, registro, locks) y `factory status` |
| `config/` | configuración central del supervisor (sin secretos) |
| `adapters/supervisor/` | envoltorio de compatibilidad 1.0 |
| `tools/` | validadores |
| `.github/workflows/` | CI de la fábrica, gate reutilizable del supervisor y aviso de triggers |
| `docs/supervisor/` | supervisor automático: operación, discovery y revisión de seguridad |
| `docs/INIT_NEW_PROJECT.md` | cómo aplicar la fábrica a un proyecto nuevo o existente |

## Cambios a la fábrica
Se versionan en `VERSION` y `CHANGELOG.md`. Un cambio de política es un PR con motivo y evidencia. Las políticas nuevas entran solo cuando un problema real las pide.
