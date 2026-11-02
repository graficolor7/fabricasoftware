# Tareas, quality gates y definición de terminado

## Ciclo
PLAN → DELEGATE → IMPLEMENT → VERIFY → INTEGRATE → AUDIT → DOCUMENT → SUPERVISE → CLOSE.
Nunca PROMPT → CODE → "DONE".

## Task Contract
Toda tarea delegada tiene un contrato válido según `schemas/task.schema.json`. Un agente no amplía su alcance en silencio. En GitHub, un issue con la plantilla de tarea es el contrato.

## Aislamiento
- Una tarea = una rama = un PR pequeño.
- Dos agentes nunca modifican los mismos archivos a la vez. Cada proyecto declara zonas exclusivas en su perfil.
- Antes de paralelizar, el Director comprueba dependencias.

## Gates universales
| Gate | Entrada | Trabajo | Evidencia | Salida | Autoridad |
|---|---|---|---|---|---|
| G0 DISCOVERY | proyecto o repo nuevo | inventario del estado real | informe F0 | estado reconstruido y aprobado | PO |
| G1 REQUIREMENTS | G0 | requisitos y criterios medibles | lista priorizada | criterios aprobados | PO |
| G2 ARCHITECTURE | G1 | propuesta, ADRs, contratos | spikes y mediciones | arquitectura aceptada | PO + Supervisor |
| G3 IMPLEMENTATION | tarea con contrato | código + tests | diff, tests | PR listo | Director |
| G4 VERIFICATION | G3 | CI y pruebas del dominio | salida de CI, goldens, benchmarks | todo verde | herramientas deterministas |
| G5 INTEGRATION | G4 | merge y E2E | E2E, regresión | main estable | Director |
| G6 SUPERVISOR | cierre de milestone o gate de riesgo | Supervisor Packet | veredicto | PASS / PASS_WITH_ACTIONS | Supervisor |
| G7 RELEASE | G6 | build y prueba en hardware real | instalador probado | release | **PO** |

Cada proyecto puede añadir gates propios (por ejemplo G4-WINDOWS).

## Evidencia antes que afirmaciones
"Funciona" exige evidencia: test unitario, integración, E2E, golden, regresión visual, benchmark, profiling, fixture, reproducción, salida de build, diff, commit o escaneo de seguridad.

## DONE
implementado + probado + integrado + documentado + evidencia registrada + protegido contra regresión + revisión requerida pasada.

## VERIFIED
Evidencia reproducible. Un agente no se autoasigna VERIFIED por confianza.

## Cierre de milestone
Criterios cumplidos, tests completos, evidencia existente, fallos críticos resueltos, documentación y contratos sincronizados, commit identificado, gate del supervisor si aplica, hallazgos bloqueantes resueltos, registro de cierre (`templates/MILESTONE_CLOSURE.md`).
Un milestone cerrado con evidencia válida no se reabre automáticamente: se abre un hallazgo o CR.

## Errores y causa raíz
Errores con valor histórico van con `templates/ERROR.md`. Un bug importante no está resuelto solo porque desapareció: ¿por qué pasó?, ¿por qué no lo detectaron los tests?, ¿qué impide que vuelva?

## Cambios a decisiones
ADRs (`templates/ADR.md`) con estados PROPOSED / ACCEPTED / SUPERSEDED / REJECTED; nunca se borra la historia. Contratos congelados solo cambian por Change Request (`schemas/change-request.schema.json`).

## Investigación
Solo ante un RESEARCH_BLOCKER o una decisión que necesite evidencia externa, y después de buscar lo que ya existe.

## Aprobación humana obligatoria
Cambio mayor de alcance, reemplazo de arquitectura, migración destructiva, ruptura de contrato con impacto amplio, release a producción, operación irreversible de datos, excepción de seguridad, desacuerdo técnico mayor.
