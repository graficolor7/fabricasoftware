# Supervisor independiente

## Jerarquía
HUMAN PRODUCT OWNER → SUPERVISOR (ChatGPT) ↔ DIRECTOR (Claude Opus) → agentes → herramientas deterministas → código/tests/evidencia.
El Product Owner tiene la autoridad final.

## Cuándo se consulta (y cuándo no)
Obligatorio: arquitectura inicial, cambios mayores de arquitectura, congelación o cambio mayor de contratos compartidos, cierre de milestone, cambios críticos de seguridad, fallos críticos sin resolver, congelación del formato de archivo / modelo de documento / arquitectura de render (o equivalentes del proyecto), pre-release, y tareas con RISK = CRITICAL.
**No** se consulta en cada commit ni en cada PR pequeño: eso lo cubre CI.

## Flujo
1. El Director arma el Supervisor Packet (`schemas/supervisor-packet.schema.json`). Sin repo completo ni secretos.
2. `adapters/supervisor/supervisor.py` valida el paquete, llama al proveedor y valida el veredicto.
3. Reglas deterministas sobre el veredicto: un hallazgo CRITICAL o bloqueante fuerza BLOCKED; PASS con acciones obligatorias pasa a PASS_WITH_ACTIONS.
4. Si pide evidencia (`EVIDENCE_REQUESTED`), el Director entrega **solo** lo pedido en `attached_evidence` y se repite.
5. El resultado se publica en el PR o issue del gate y se guarda en el repo del proyecto (`.factory/verdicts/`).

## Semántica
| Veredicto | Efecto |
|---|---|
| PASS | el gate puede cerrar |
| PASS_WITH_ACTIONS | avanza si las acciones no son bloqueantes; las acciones se convierten en tareas |
| BLOCKED | no cierra; se resuelven los hallazgos bloqueantes y se vuelve a auditar |

## Anti-bucle
- Máximo 3 rondas de auditoría por gate.
- Si Director y Supervisor discrepan 2 veces sobre el mismo hallazgo: `templates/DISAGREEMENT_REPORT.md` al Product Owner y se detiene el gasto en ese tema.
- El supervisor no bloquea por preferencias (ver prompt).

## Operación degradada
| Estado | Cuándo | Efecto |
|---|---|---|
| SUPERVISOR_PENDING | proveedor caído, sin clave o sin modelo | el gate queda abierto; el trabajo que no depende del gate sigue |
| MODEL_UNAVAILABLE | modelo del constructor no disponible | se usa la clase inferior solo para L0–L2 |
| DEGRADED_MODE | varios proveedores con fallos | solo trabajo determinista |
| HUMAN_REVIEW_REQUIRED | respuesta inválida del supervisor tras reintento | revisión del PO |
Un fallo de API nunca cambia el estado del proyecto ni borra evidencia.
